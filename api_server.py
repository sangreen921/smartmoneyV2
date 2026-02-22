"""
SmartMoney API服务器 - 完整修复版
1. 集成pricing模块配额检查
2. 添加订阅升级API
3. 修复所有路由
"""
from flask import Flask, jsonify, request, send_from_directory, redirect
try:
    from flask_cors import CORS
    HAS_CORS = True
except ImportError:
    HAS_CORS = False
import sqlite3
import requests
from datetime import datetime
import asyncio
import os
import sys
import time
import random
import hashlib
from io import BytesIO
import base64

# 可选依赖：二维码生成
try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False
    print("⚠️  qrcode库未安装，二维码功能将使用备用方案")

# 导入数据库初始化
from database import init_database

# 导入样例数据
from sample_data import get_sample_data

# 导入认证模块
from auth import (
    init_auth_db, optional_auth, require_auth,
    register_user, login_user, get_google_auth_url, google_callback
)

# 导入定价模块
from pricing import (
    init_pricing_tables,
    check_quota,
    increment_usage,
    get_all_plans,
    get_usage_stats,
    upgrade_plan
)

# 导入原始扩展（如果存在）
try:
    from database_extended import (
        get_transfer_history,
        get_subscribers,
        upsert_notification_subscription,
        delete_subscription,
        extend_database
    )
    from telegram_notifier import TelegramNotifier
    HAS_EXTENSIONS = True
    extend_database()
except ImportError:
    HAS_EXTENSIONS = False
    print("⚠️  扩展模块未找到，部分功能将不可用")

app = Flask(__name__, static_folder='.', static_url_path='')
if HAS_CORS:
    CORS(app)

DB_NAME = "smartmoney_v2.db"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "binance_user_data")

# ✅ 演示模式配置
DEMO_MODE = os.getenv('DEMO_MODE', 'false').lower() == 'true'
CRYPTO_WALLET_ADDRESS = os.getenv('CRYPTO_WALLET_ADDRESS', 'TRXexampleAddressForUSDT12345678901234')

if DEMO_MODE:
    print("📢 演示模式已启用 (DEMO_MODE=true)")
    print("   - 添加交易员将使用模拟数据，不调用Binance API")
    print("   - 适用于测试和演示环境")

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def migrate_database():
    """数据库迁移：添加缺失的字段"""
    conn = get_db()
    cursor = conn.cursor()
    
    migrations = []
    
    try:
        # 检查payment_orders表是否存在user_wallet字段
        cursor.execute("PRAGMA table_info(payment_orders)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'user_wallet' not in columns:
            migrations.append(("payment_orders", "user_wallet", "TEXT"))
            print("🔧 检测到缺失字段: payment_orders.user_wallet")
    except sqlite3.OperationalError:
        # 表不存在，跳过
        pass
    
    # 执行迁移
    for table, column, col_type in migrations:
        try:
            print(f"🔧 迁移: {table}.{column} ({col_type})")
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.commit()
            print(f"✅ 迁移成功: {table}.{column}")
        except Exception as e:
            print(f"⚠️  迁移失败: {table}.{column} - {e}")
    
    conn.close()
    
    if migrations:
        print(f"✅ 数据库迁移完成 ({len(migrations)}个字段)")
    else:
        print("✅ 数据库结构最新")

# ============================================================
# 前端页面路由
# ============================================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/detail.html')
def detail():
    return send_from_directory('static', 'detail.html')

@app.route('/login.html')
def login_page():
    return send_from_directory('static', 'login.html')

@app.route('/test_login.html')
def test_login():
    return send_from_directory('static', 'test_login.html')

@app.route('/admin.html')
def admin_page():
    return send_from_directory('static', 'admin.html')

# ============================================================
# 认证API
# ============================================================

@app.route('/api/auth/register', methods=['POST'])
def api_register():
    data = request.get_json()
    result = register_user(
        data.get('email'),
        data.get('password'),
        data.get('username')
    )
    return jsonify(result), 200 if result['success'] else 400

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    data = request.get_json()
    result = login_user(data.get('email'), data.get('password'))
    return jsonify(result), 200 if result['success'] else 401

@app.route('/api/auth/me', methods=['GET'])
@require_auth
def api_get_me():
    user = request.current_user
    
    # 获取使用统计
    usage = get_usage_stats(user['id'])
    
    return jsonify({
        'success': True,
        'user': {
            'id': user['id'],
            'email': user['email'],
            'username': user['username'],
            'avatar_url': user.get('avatar_url'),
            'subscription_plan': user['subscription_plan'],
            'max_traders': user['max_traders'],
            'max_notifications_daily': user['max_notifications_daily']
        },
        'usage': usage
    })

@app.route('/api/auth/google/url', methods=['GET'])
def api_google_url():
    url = get_google_auth_url()
    if url:
        return jsonify({'success': True, 'url': url})
    else:
        return jsonify({'success': False, 'error': 'Google OAuth未配置'}), 400

@app.route('/api/auth/google/callback', methods=['GET'])
def api_google_callback():
    code = request.args.get('code')
    if not code:
        return jsonify({'success': False, 'error': '缺少授权码'}), 400
    
    result = google_callback(code)
    if result['success']:
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:5000')
        return redirect(f"{frontend_url}/?token={result['token']}")
    else:
        return jsonify(result), 400

# ============================================================
# 定价和订阅API
# ============================================================

@app.route('/api/pricing/plans', methods=['GET'])
def api_get_plans():
    """获取所有套餐"""
    plans = get_all_plans()
    return jsonify({
        'success': True,
        'data': plans
    })

@app.route('/api/pricing/usage', methods=['GET'])
@require_auth
def api_get_usage():
    """获取用户使用统计"""
    user = request.current_user
    usage = get_usage_stats(user['id'])
    return jsonify({
        'success': True,
        'data': usage
    })

@app.route('/api/pricing/upgrade', methods=['POST'])
@require_auth
def api_upgrade():
    """升级套餐 - 仅演示模式可用"""
    
    # ✅ 安全检查：仅在演示模式允许直接升级
    if not DEMO_MODE:
        return jsonify({
            'success': False,
            'error': '升级需要通过支付流程完成',
            'hint': '请点击"升级"按钮，完成支付后自动升级',
            'payment_required': True
        }), 403
    
    # 演示模式：允许直接升级（用于测试）
    user = request.current_user
    data = request.get_json()
    plan_code = data.get('plan_code')
    
    if not plan_code:
        return jsonify({'success': False, 'error': '缺少套餐代码'}), 400
    
    result = upgrade_plan(user['id'], plan_code)
    result['demo_mode'] = True  # 标记为演示模式升级
    return jsonify(result)

# ============================================================
# 商业化API - 样例数据、加密货币支付
# ============================================================

# 配置：加密货币收款地址（应该从环境变量读取）
CRYPTO_WALLET_ADDRESS = os.getenv('CRYPTO_WALLET_ADDRESS', 'TRXexampleAddressForUSDT12345678901234')

@app.route('/api/sample-data', methods=['GET'])
def api_sample_data():
    """获取样例数据（未登录用户展示）"""
    data = get_sample_data()
    return jsonify({
        'success': True,
        **data
    })

@app.route('/api/payment/create', methods=['POST'])
@require_auth
def create_payment_order():
    """创建加密货币支付订单 - 优化版"""
    user = request.current_user
    data = request.get_json()
    
    # 使用钱包地址替代邮箱
    user_wallet = data.get('user_wallet', '')
    plan_code = data.get('plan_code', 'annual')
    
    # 验证钱包地址
    if not user_wallet or len(user_wallet) < 10:
        return jsonify({'success': False, 'error': '请输入有效的钱包地址'}), 400
    
    # 生成订单号
    timestamp = int(time.time())
    random_suffix = random.randint(1000, 9999)
    order_id = f"ORD{timestamp}{random_suffix}"
    
    # 订单金额
    amount = 12.99  # 年付版固定价格
    
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO payment_orders 
            (order_id, user_email, user_wallet, plan_code, amount_usd, crypto_address, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (order_id, user['email'], user_wallet, plan_code, amount, CRYPTO_WALLET_ADDRESS))
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'order_id': order_id,
            'amount': amount,
            'crypto_address': CRYPTO_WALLET_ADDRESS,
            'qr_url': f'/api/payment/qr/{order_id}',
            'plan': {
                'name': '年付版',
                'code': 'annual',
                'price': 12.99,
                'features': ['100个交易员', 'Telegram通知不限', '优先客户支持', '数据导出功能']
            }
        })
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/payment/qr/<order_id>', methods=['GET'])
def get_payment_qr(order_id):
    """生成支付二维码"""
    conn = get_db()
    order = conn.execute("""
        SELECT crypto_address, amount_usd FROM payment_orders 
        WHERE order_id = ?
    """, (order_id,)).fetchone()
    conn.close()
    
    if not order:
        return "Order not found", 404
    
    # 生成包含地址和金额的二维码
    qr_content = f"{order['crypto_address']}"
    
    if HAS_QRCODE:
        # 使用qrcode库生成
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(qr_content)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # 转为base64
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        img_str = base64.b64encode(buffer.read()).decode()
        qr_image_html = f'<img src="data:image/png;base64,{img_str}" alt="Payment QR Code" />'
    else:
        # 备用方案：使用在线二维码API
        qr_api_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={qr_content}"
        qr_image_html = f'<img src="{qr_api_url}" alt="Payment QR Code" onerror="this.style.display=\'none\'" />'
    
    # 返回HTML页面显示二维码
    html = f'''
    <!DOCTYPE html>
    <html>
    <head><title>支付二维码</title>
    <style>
        body {{font-family: sans-serif; text-align: center; padding: 40px; background: #f5f5f5;}}
        .container {{max-width: 500px; margin: 0 auto; background: white; padding: 30px; border-radius: 12px;}}
        img {{max-width: 300px; border: 2px solid #ddd; padding: 10px; background: white;}}
        .info {{margin: 20px 0; font-size: 14px; color: #555;}}
        .address {{background: #f0f0f0; padding: 12px; border-radius: 6px; word-break: break-all; font-family: monospace;}}
        .copy-btn {{margin-top: 10px; padding: 10px 20px; background: #1890ff; color: white; border: none; border-radius: 4px; cursor: pointer;}}
    </style>
    </head>
    <body>
        <div class="container">
            <h2>扫码支付 ${order['amount_usd']} USDT</h2>
            {qr_image_html}
            <div class="info">
                <p><strong>钱包地址：</strong></p>
                <div class="address" id="address">{order['crypto_address']}</div>
                <button class="copy-btn" onclick="copyAddress()">复制地址</button>
                <p style="margin-top:20px;"><strong>网络：</strong> TRC20 (Tron)</p>
                <p><strong>订单号：</strong> {order_id}</p>
                <p style="color:#999; font-size:12px; margin-top:20px;">⚠️ 付款后请联系管理员确认</p>
            </div>
        </div>
        <script>
        function copyAddress() {{
            const text = document.getElementById('address').innerText;
            navigator.clipboard.writeText(text).then(() => {{
                alert('地址已复制！');
            }}).catch(() => {{
                // 备用复制方法
                const textarea = document.createElement('textarea');
                textarea.value = text;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);
                alert('地址已复制！');
            }});
        }}
        </script>
    </body>
    </html>
    '''
    return html

@app.route('/api/payment/status/<order_id>', methods=['GET'])
def check_payment_status(order_id):
    """查询支付状态"""
    conn = get_db()
    order = conn.execute("""
        SELECT status, paid_at FROM payment_orders 
        WHERE order_id = ?
    """, (order_id,)).fetchone()
    conn.close()
    
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    
    return jsonify({
        'success': True,
        'order_id': order_id,
        'status': order['status'],
        'paid_at': order['paid_at']
    })

@app.route('/api/admin/payments/pending', methods=['GET'])
@require_auth
def get_pending_payments():
    """获取待确认支付列表（管理员）"""
    user = request.current_user
    
    # 简单检查：只允许特定管理员邮箱
    admin_emails_str = os.getenv('ADMIN_EMAILS', '')
    if not admin_emails_str:
        return jsonify({'success': False, 'error': '未配置管理员邮箱'}), 403
    
    admin_emails = [e.strip() for e in admin_emails_str.split(',') if e.strip()]
    if user['email'] not in admin_emails:
        return jsonify({'success': False, 'error': '无权限'}), 403
    
    conn = get_db()
    orders = conn.execute("""
        SELECT * FROM payment_orders 
        WHERE status = 'pending'
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    
    return jsonify({
        'success': True,
        'orders': [dict(row) for row in orders]
    })

@app.route('/api/admin/payment/confirm', methods=['POST'])
@require_auth
def confirm_payment():
    """确认支付并升级账户（管理员）"""
    user = request.current_user
    data = request.get_json()
    order_id = data.get('order_id')
    
    # 检查管理员权限
    admin_emails_str = os.getenv('ADMIN_EMAILS', '')
    if not admin_emails_str:
        return jsonify({'success': False, 'error': '未配置管理员邮箱'}), 403
    
    admin_emails = [e.strip() for e in admin_emails_str.split(',') if e.strip()]
    if user['email'] not in admin_emails:
        return jsonify({'success': False, 'error': '无权限'}), 403
    
    if not order_id:
        return jsonify({'success': False, 'error': '缺少订单号'}), 400
    
    conn = get_db()
    
    # 查询订单
    order = conn.execute("""
        SELECT * FROM payment_orders WHERE order_id = ?
    """, (order_id,)).fetchone()
    
    if not order:
        conn.close()
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    
    if order['status'] == 'paid':
        conn.close()
        return jsonify({'success': False, 'error': '订单已确认'}), 400
    
    try:
        # 标记订单已支付
        now = datetime.now().isoformat()
        conn.execute("""
            UPDATE payment_orders 
            SET status = 'paid', 
                paid_at = ?,
                confirmed_by = ?,
                confirmed_at = ?
            WHERE order_id = ?
        """, (now, user['email'], now, order_id))
        
        # 升级用户账户
        conn.execute("""
            UPDATE users 
            SET subscription_plan = 'annual',
                max_traders = 100,
                max_notifications_daily = 9999
            WHERE email = ?
        """, (order['user_email'],))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'订单 {order_id} 已确认，用户 {order["user_email"]} 已升级到年付版'
        })
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# 交易员API（保留原功能，添加配额检查）
# ============================================================

@app.route('/api/traders', methods=['GET'])
@require_auth
def get_traders():
    """获取交易员列表（需要登录，只返回当前用户的交易员）"""
    user = request.current_user
    conn = get_db()
    traders = conn.execute("""
        SELECT 
            top_trader_id, trader_name, account_name, avatar_url,
            roi, pnl, um_margin_balance, win_rate,
            subscribers, days_active, last_update
        FROM traders
        WHERE user_id = ?
        ORDER BY last_update DESC
    """, (user['id'],)).fetchall()
    conn.close()
    
    return jsonify({
        'success': True,
        'data': [dict(row) for row in traders]
    })

@app.route('/api/traders', methods=['POST'])
@require_auth
def add_trader():
    """添加交易员（需要登录 + 配额检查）"""
    user = request.current_user
    data = request.get_json()
    trader_id = data.get('trader_id')
    
    if not trader_id:
        return jsonify({'success': False, 'error': '缺少 trader_id'}), 400
    
    # ✅ 配额检查
    quota = check_quota(user['id'], 'traders')
    if not quota['allowed']:
        return jsonify({
            'success': False,
            'error': f'已达到交易员数量上限 ({quota["current"]}/{quota["limit"]})',
            'quota_exceeded': True,
            'current': quota['current'],
            'limit': quota['limit']
        }), 403
    
    # 检查是否已存在
    conn = get_db()
    existing = conn.execute(
        "SELECT top_trader_id FROM traders WHERE top_trader_id = ? AND user_id = ?",
        (trader_id, user['id'])
    ).fetchone()
    
    if existing:
        conn.close()
        return jsonify({'success': False, 'error': '该交易员已在监控列表中'}), 400
    
    # ✅ DEMO模式：使用模拟数据
    if DEMO_MODE:
        try:
            conn.execute("""
            INSERT INTO traders (
                top_trader_id, trader_name, account_name, avatar_url, introduction,
                roi, pnl, um_margin_balance, cm_margin_balance,
                days_active, max_drawdown, win_rate, net_transfer,
                subscribers, sharing_position, sharing_history,
                sharing_latest_record, is_ai, last_update, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trader_id,
                f"Demo Trader {trader_id[-6:]}",
                f"账户{trader_id[-4:]}",
                "",  # avatar_url
                "演示模式模拟交易员",
                15.5,  # roi
                1250.0,  # pnl
                10000.0,  # um_margin_balance
                5000.0,  # cm_margin_balance
                30,  # days_active
                -8.5,  # max_drawdown
                65.0,  # win_rate
                0.0,  # net_transfer
                100,  # subscribers
                1, 1, 1, 0,  # sharing flags
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                user['id']
            ))
            
            conn.commit()
            conn.close()
            
            return jsonify({
                'success': True,
                'message': f'成功添加交易员: Demo Trader {trader_id[-6:]}',
                'demo_mode': True,
                'quota': {
                    'current': quota['current'] + 1,
                    'limit': quota['limit']
                }
            })
        except Exception as e:
            conn.close()
            return jsonify({'success': False, 'error': f'数据库错误: {str(e)}'}), 500
    
    # 生产模式：抓取Binance数据
    try:
        profile_api = f"https://www.binance.com/bapi/asset/v1/friendly/future/smart-money/profile?topTraderId={trader_id}"
        resp = requests.get(profile_api, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        
        if not result.get('success'):
            conn.close()
            return jsonify({'success': False, 'error': 'Binance API 返回失败'}), 500
        
        raw = result['data']
        
        # 插入数据库（含user_id，关联当前登录用户）
        conn.execute("""
        INSERT INTO traders (
            top_trader_id, trader_name, account_name, avatar_url, introduction,
            roi, pnl, um_margin_balance, cm_margin_balance,
            days_active, max_drawdown, win_rate, net_transfer,
            subscribers, sharing_position, sharing_history,
            sharing_latest_record, is_ai, last_update, user_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            raw.get("topTraderId"),
            raw.get("traderName"),
            raw.get("accountName"),
            raw.get("avatarUrl"),
            raw.get("introduction"),
            float(raw.get("roi", 0)),
            float(raw.get("pnl", 0)),
            float(raw.get("umMarginBalance", 0)),
            float(raw.get("cmMarginBalance", 0)),
            int(raw.get("daysActive", 0)),
            float(raw.get("mdd", 0)),
            float(raw.get("winRate", 0)),
            float(raw.get("netTransfer", 0)),
            int(raw.get("subscribers", 0)),
            1 if raw.get("sharingPosition") else 0,
            1 if raw.get("sharingPositionHistory") else 0,
            1 if raw.get("sharingLatestRecord") else 0,
            1 if raw.get("isAi") else 0,
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            user['id']
        ))
        
        conn.commit()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'成功添加交易员: {raw.get("traderName")}',
            'quota': {
                'current': quota['current'] + 1,
                'limit': quota['limit']
            }
        })
        
    except requests.RequestException as e:
        conn.close()
        return jsonify({
            'success': False,
            'error': '无法连接到Binance服务器，请检查网络连接',
            'detail': str(e),
            'hint': '测试环境可设置环境变量 DEMO_MODE=true 使用模拟数据'
        }), 503
    except Exception as e:
        conn.close()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/traders/<trader_id>', methods=['DELETE'])
@require_auth
def delete_trader(trader_id):
    """删除交易员（需要登录，只能删除自己的交易员）"""
    user = request.current_user
    conn = get_db()
    
    # 验证所有权
    trader = conn.execute(
        "SELECT * FROM traders WHERE top_trader_id = ? AND user_id = ?",
        (trader_id, user['id'])
    ).fetchone()
    
    if not trader:
        conn.close()
        return jsonify({'success': False, 'error': '交易员不存在或无权删除'}), 403
    
    conn.execute("DELETE FROM traders WHERE top_trader_id = ?", (trader_id,))
    conn.execute("DELETE FROM positions_current WHERE top_trader_id = ?", (trader_id,))
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': '交易员已删除'})

@app.route('/api/traders/<trader_id>', methods=['GET'])
@require_auth
def get_trader_detail(trader_id):
    """获取交易员详情（需要登录，只能查看自己的交易员）"""
    user = request.current_user
    conn = get_db()
    trader = conn.execute("""
        SELECT * FROM traders 
        WHERE top_trader_id = ? AND user_id = ?
    """, (trader_id, user['id'])).fetchone()
    conn.close()
    
    if not trader:
        return jsonify({'success': False, 'error': '交易员不存在或无权访问'}), 403
    
    return jsonify({
        'success': True,
        'data': dict(trader)
    })

# ============================================================
# 持仓API
# ============================================================

@app.route('/api/positions/<trader_id>', methods=['GET'])
@require_auth
def get_positions(trader_id):
    """获取持仓（需要登录，只能查看自己的交易员）"""
    user = request.current_user
    conn = get_db()
    
    # 验证所有权
    trader = conn.execute(
        "SELECT 1 FROM traders WHERE top_trader_id = ? AND user_id = ?",
        (trader_id, user['id'])
    ).fetchone()
    
    if not trader:
        conn.close()
        return jsonify({'success': False, 'error': '无权访问该交易员数据'}), 403
    
    positions = conn.execute("""
        SELECT * FROM positions_current
        WHERE top_trader_id = ?
        ORDER BY position_value DESC
    """, (trader_id,)).fetchall()
    conn.close()
    
    return jsonify({
        'success': True,
        'data': [dict(row) for row in positions]
    })

@app.route('/api/positions/<trader_id>/history', methods=['GET'])
@require_auth
def get_position_history(trader_id):
    """获取持仓历史（需要登录，只能查看自己的交易员）"""
    user = request.current_user
    conn = get_db()
    
    # 验证所有权
    trader = conn.execute(
        "SELECT 1 FROM traders WHERE top_trader_id = ? AND user_id = ?",
        (trader_id, user['id'])
    ).fetchone()
    
    if not trader:
        conn.close()
        return jsonify({'success': False, 'error': '无权访问该交易员数据'}), 403
    
    rows = conn.execute("""
        SELECT 
            snapshot_time,
            SUM(position_value) as total_value,
            SUM(unrealized_pnl) as total_pnl
        FROM positions_history
        WHERE top_trader_id = ?
        GROUP BY snapshot_time
        ORDER BY snapshot_time ASC
    """, (trader_id,)).fetchall()
    conn.close()
    
    return jsonify({
        'success': True,
        'data': [dict(row) for row in rows]
    })

# ============================================================
# 交易记录API
# ============================================================

@app.route('/api/trades/<trader_id>', methods=['GET'])
@require_auth
def get_trades(trader_id):
    """获取交易记录（需要登录，只能查看自己的交易员）"""
    user = request.current_user
    limit = request.args.get('limit', 100, type=int)
    offset = request.args.get('offset', 0, type=int)
    
    conn = get_db()
    
    # 验证所有权
    trader = conn.execute(
        "SELECT 1 FROM traders WHERE top_trader_id = ? AND user_id = ?",
        (trader_id, user['id'])
    ).fetchone()
    
    if not trader:
        conn.close()
        return jsonify({'success': False, 'error': '无权访问该交易员数据'}), 403
    
    trades = conn.execute("""
        SELECT symbol, side, price, amount, trade_time
        FROM trade_records
        WHERE top_trader_id = ?
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """, (trader_id, limit, offset)).fetchall()
    
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM trade_records WHERE top_trader_id = ?",
        (trader_id,)
    ).fetchone()['cnt']
    
    conn.close()
    
    return jsonify({
        'success': True,
        'data': [dict(row) for row in trades],
        'total': total
    })

# ============================================================
# 扩展API（如果有）
# ============================================================

if HAS_EXTENSIONS:
    @app.route('/api/transfers/<trader_id>', methods=['GET'])
    def api_get_transfers(trader_id):
        transfers = get_transfer_history(trader_id)
        return jsonify({'success': True, 'data': transfers})
    
    @app.route('/api/subscribers/<trader_id>', methods=['GET'])
    def api_get_subscribers(trader_id):
        subscribers = get_subscribers(trader_id)
        return jsonify({'success': True, 'data': subscribers})

# ============================================================
# 启动服务器
# ============================================================

if __name__ == '__main__':
    # 初始化数据库
    print("初始化数据库...")
    init_database()      # 原始业务表
    init_auth_db()       # 认证表
    init_pricing_tables()  # 定价表
    print("✅ 数据库初始化完成")
    
    # 数据库迁移
    migrate_database()   # ✅ 添加迁移逻辑
    
    print("\n" + "="*60)
    print("  SmartMoney Pro - 完整版")
    print("="*60)
    print("  主页:   http://localhost:5000/")
    print("  登录:   http://localhost:5000/login.html")
    print("  功能:   ✅ 配额检查 ✅ 订阅升级")
    print("="*60 + "\n")
    
    app.run(host='0.0.0.0', port=5000, debug=True)