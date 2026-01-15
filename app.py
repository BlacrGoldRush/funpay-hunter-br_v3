import os
import logging
import requests
import re
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from datetime import datetime
import threading
import time
import schedule
from telegram import Bot
from telegram.error import TelegramError

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Конфигурация из переменных окружения
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# Проверка конфигурации
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.error("❌ Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID")
    raise ValueError("Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в переменных окружения")

# Инициализация бота
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Глобальные переменные
found_items = {}
monitoring_active = False
monitoring_thread = None

def send_telegram_message(message, parse_mode='HTML'):
    """Отправка сообщения в Telegram"""
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=parse_mode)
        logger.info(f"📨 Отправлено в Telegram: {message[:50]}...")
        return True
    except TelegramError as e:
        logger.error(f"❌ Ошибка Telegram: {e}")
        return False

def smart_parse_black_russia(url, category):
    """Парсинг Black Russia ТОЛЬКО с онлайн продавцами"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Referer': 'https://funpay.com/',
        }
        
        logger.info(f"🎮 Парсинг {category} (только онлайн продавцы)...")
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"❌ Ошибка HTTP: {response.status_code}")
            return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Ищем ВСЕ карточки товаров
        product_cards = soup.find_all('div', class_='tc-item')
        logger.info(f"📦 Найдено карточек: {len(product_cards)}")
        
        items = []
        online_count = 0
        offline_count = 0
        
        for card in product_cards[:40]:  # Анализируем первые 40
            try:
                # 1. Проверяем статус продавца (ОНЛАЙН/ОФФЛАЙН)
                seller_online = False
                
                # Ищем статус продавца
                status_elem = card.find('div', class_='media-user-status')
                if status_elem:
                    status_text = status_elem.get_text(strip=True)
                    if 'Онлайн' in status_text or 'online' in status_text.lower():
                        seller_online = True
                        online_count += 1
                    else:
                        offline_count += 1
                        continue  # Пропускаем офлайн
                else:
                    # Если статус не найден, пропускаем для безопасности
                    continue
                
                # 2. Извлекаем название товара
                title_elem = card.find('div', class_='tc-desc-text')
                if not title_elem:
                    continue
                
                title = title_elem.get_text(strip=True)
                
                # 3. Фильтруем ТОЛЬКО Black Russia
                title_lower = title.lower()
                keywords = [
                    'black russia', 
                    'blackrussia', 
                    'блек раша',
                    'блек рашн',
                    'блэк раша',
                    'br ',
                    'бр ',
                    'black s',
                    'blacks'
                ]
                
                if not any(keyword in title_lower for keyword in keywords):
                    continue
                
                # 4. Извлекаем цену
                price_elem = card.find('div', class_='tc-price')
                if not price_elem:
                    continue
                
                price_text = price_elem.get_text(strip=True)
                
                # Извлекаем цифры из цены
                digits = re.findall(r'\d+', price_text.replace(' ', ''))
                if not digits:
                    continue
                
                price = int(''.join(digits))
                
                # Фильтр по цене (от 10 до 50000 руб)
                if price < 10 or price > 50000:
                    continue
                
                # 5. Извлекаем ссылку на товар
                link = url
                link_elem = card.find('a')
                if link_elem and link_elem.get('href'):
                    href = link_elem['href']
                    if href.startswith('/'):
                        link = f"https://funpay.com{href}"
                    elif href.startswith('http'):
                        link = href
                
                # 6. Создаем уникальный ID для товара
                item_id = f"{hash(title)}_{price}_{hash(link)}"
                
                # 7. Добавляем товар в список
                items.append({
                    'id': item_id,
                    'title': title[:100],
                    'price': price,
                    'link': link,
                    'category': category,
                    'seller_online': seller_online
                })
                
                logger.info(f"   ✅ [ОНЛАЙН] '{title[:50]}...' - {price} руб.")
                
            except Exception as e:
                logger.warning(f"⚠️ Ошибка обработки карточки: {e}")
                continue
        
        logger.info(f"📊 Статистика парсинга:")
        logger.info(f"   • Онлайн продавцов: {online_count}")
        logger.info(f"   • Офлайн продавцов: {offline_count}")
        logger.info(f"   • Black Russia товаров: {len(items)}")
        
        return items
        
    except Exception as e:
        logger.error(f"💥 Критическая ошибка парсинга: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return []

def check_new_items():
    """Проверка новых товаров и отправка уведомлений"""
    global found_items
    
    if not monitoring_active:
        return
    
    logger.info("🔍 Начинаем проверку новых товаров...")
    
    # URL для мониторинга
    urls_to_monitor = [
        ("https://funpay.com/chips/186/", "Black Russia - Вирты"),
    ]
    
    for url, category in urls_to_monitor:
        current_items = smart_parse_black_russia(url, category)
        
        # Проверяем новые товары
        for item in current_items:
            item_id = item['id']
            if item_id not in found_items:
                found_items[item_id] = item
                
                # Формируем сообщение для Telegram
                message = (
                    f"🎮 <b>НОВОЕ ПРЕДЛОЖЕНИЕ {category}</b>\n\n"
                    f"📦 <b>{item['title']}</b>\n"
                    f"💰 <b>Цена:</b> {item['price']} руб.\n"
                    f"🟢 <b>Статус:</b> Продавец онлайн\n"
                    f"🔗 <a href='{item['link']}'>Открыть на FunPay</a>\n\n"
                    f"⏰ {datetime.now().strftime('%H:%M:%S')}"
                )
                send_telegram_message(message)
    
    logger.info(f"📊 Всего отслеживаемых товаров: {len(found_items)}")

def monitoring_loop():
    """Цикл мониторинга"""
    global monitoring_active
    
    logger.info("🔄 Запуск цикла мониторинга...")
    
    while monitoring_active:
        try:
            check_new_items()
            # Ждем 60 секунд перед следующей проверкой
            for _ in range(60):
                if not monitoring_active:
                    break
                time.sleep(1)
        except Exception as e:
            logger.error(f"❌ Ошибка в цикле мониторинга: {e}")
            time.sleep(30)

# Маршруты Flask
@app.route('/')
def index():
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>FunPay Hunter для Black Russia</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
            .container {{ max-width: 800px; margin: 0 auto; }}
            .status {{ padding: 20px; border-radius: 10px; margin: 20px 0; }}
            .online {{ background: #d4edda; border: 1px solid #c3e6cb; }}
            .offline {{ background: #f8d7da; border: 1px solid #f5c6cb; }}
            .btn {{ display: inline-block; padding: 10px 20px; background: #007bff; 
                   color: white; text-decoration: none; border-radius: 5px; margin: 5px; }}
            .btn:hover {{ background: #0056b3; }}
            .btn-red {{ background: #dc3545; }}
            .btn-red:hover {{ background: #c82333; }}
            .btn-green {{ background: #28a745; }}
            .btn-green:hover {{ background: #218838; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 FunPay Hunter для Black Russia</h1>
            <div class="status {'online' if monitoring_active else 'offline'}">
                <h3>{'✅ Сервис работает' if monitoring_active else '⏸️ Сервис остановлен'}</h3>
                <p><strong>Мониторинг:</strong> {'🟢 АКТИВЕН' if monitoring_active else '🔴 ОСТАНОВЛЕН'}</p>
                <p><strong>Найдено товаров:</strong> {len(found_items)}</p>
                <p><strong>Время:</strong> {datetime.now().strftime('%H:%M:%S')}</p>
            </div>
            <div>
                <a href="/test" class="btn">🔍 Тест парсинга</a>
                <a href="/start_monitor" class="btn btn-green">▶️ Запустить мониторинг</a>
                <a href="/stop_monitor" class="btn btn-red">⏹️ Остановить мониторинг</a>
                <a href="/check" class="btn">🔄 Проверить сейчас</a>
                <a href="/status" class="btn">📊 Статус</a>
            </div>
            <div style="margin-top: 30px;">
                <h3>📋 Инструкция:</h3>
                <ol>
                    <li>Нажмите "Тест парсинга" для проверки работы парсера</li>
                    <li>Запустите мониторинг кнопкой "Запустить мониторинг"</li>
                    <li>Бот будет присылать новые предложения в Telegram каждые 60 секунд</li>
                    <li>Получайте уведомления только от онлайн продавцов</li>
                </ol>
                <p><strong>Telegram команды:</strong> /start, /check, /monitor, /stop, /status, /help</p>
            </div>
        </div>
    </body>
    </html>
    """

@app.route('/test')
def test():
    """Тестовая страница для проверки парсинга"""
    try:
        url = "https://funpay.com/chips/186/"
        items = smart_parse_black_russia(url, "Black Russia - Вирты")
        
        if items:
            result = f"<h2>✅ Найдено {len(items)} товаров Black Russia:</h2>"
            for item in items:
                result += f"""
                <div style="border: 1px solid #ddd; padding: 15px; margin: 10px; border-radius: 5px;">
                    <h3>{item['title']}</h3>
                    <p><strong>Цена:</strong> {item['price']} руб.</p>
                    <p><strong>Статус продавца:</strong> {'🟢 Онлайн' if item['seller_online'] else '🔴 Офлайн'}</p>
                    <p><strong>Ссылка:</strong> <a href="{item['link']}" target="_blank">Открыть на FunPay</a></p>
                </div>
                """
            result += f'<p><a href="/">← Назад на главную</a></p>'
        else:
            result = """
            <div style="border: 1px solid #f5c6cb; padding: 20px; border-radius: 5px; background: #f8d7da;">
                <h2>❌ Товары не найдены</h2>
                <p>Возможные причины:</p>
                <ul>
                    <li>Нет онлайн продавцов в данный момент</li>
                    <li>Изменена структура FunPay (требуется обновление парсера)</li>
                    <li>Проблемы с подключением к FunPay</li>
                    <li>На странице нет товаров Black Russia</li>
                </ul>
                <p>Проверьте логи на Render Dashboard для подробной информации.</p>
            </div>
            <p><a href="/">← Назад на главную</a></p>
            """
        
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Тест парсинга</title>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial; margin: 20px; }}
                a {{ color: #007bff; text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            {result}
        </body>
        </html>
        """
    except Exception as e:
        return f"<h2>❌ Ошибка:</h2><pre>{str(e)}</pre><p><a href='/'>← Назад</a></p>"

@app.route('/start_monitor')
def start_monitor():
    """Запуск мониторинга через браузер"""
    global monitoring_active, monitoring_thread
    
    if not monitoring_active:
        monitoring_active = True
        monitoring_thread = threading.Thread(target=monitoring_loop)
        monitoring_thread.daemon = True
        monitoring_thread.start()
        
        send_telegram_message("✅ <b>Мониторинг запущен!</b>\n\nЯ буду присылать новые предложения Black Russia каждые 60 секунд.")
        
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body { font-family: Arial; margin: 20px; }
                .success { background: #d4edda; border: 1px solid #c3e6cb; padding: 20px; border-radius: 5px; }
            </style>
        </head>
        <body>
            <div class="success">
                <h2>✅ Мониторинг запущен!</h2>
                <p>Бот начал отслеживать новые предложения Black Russia.</p>
                <p>Проверка будет выполняться каждые 60 секунд.</p>
                <p>Вы получили уведомление в Telegram.</p>
            </div>
            <p><a href="/">← Назад на главную</a></p>
        </body>
        </html>
        """
    else:
        return """
        <!DOCTYPE html>
        <html>
        <body style="font-family: Arial; margin: 20px;">
            <h2>⚠️ Мониторинг уже запущен</h2>
            <p><a href="/">← Назад на главную</a></p>
        </body>
        </html>
        """

@app.route('/stop_monitor')
def stop_monitor():
    """Остановка мониторинга"""
    global monitoring_active
    
    monitoring_active = False
    
    send_telegram_message("⏸️ <b>Мониторинг остановлен</b>")
    
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial; margin: 20px; }
            .info { background: #d1ecf1; border: 1px solid #bee5eb; padding: 20px; border-radius: 5px; }
        </style>
    </head>
    <body>
        <div class="info">
            <h2>⏸️ Мониторинг остановлен</h2>
            <p>Бот больше не будет проверять новые предложения.</p>
            <p>Вы получили уведомление в Telegram.</p>
        </div>
        <p><a href="/">← Назад на главную</a></p>
    </body>
    </html>
    """

@app.route('/check')
def manual_check():
    """Ручная проверка"""
    check_new_items()
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: Arial; margin: 20px; }}
            .info {{ background: #d1ecf1; border: 1px solid #bee5eb; padding: 20px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <div class="info">
            <h2>🔍 Проверка выполнена</h2>
            <p>Проверено на наличие новых предложений.</p>
            <p><strong>Всего отслеживаемых товаров:</strong> {len(found_items)}</p>
        </div>
        <p><a href="/">← Назад на главную</a></p>
    </body>
    </html>
    """

@app.route('/status')
def status_page():
    """Страница статуса"""
    status_text = "🟢 АКТИВЕН" if monitoring_active else "🔴 ОСТАНОВЛЕН"
    
    # Получаем последние 5 товаров
    recent_items = list(found_items.values())[-5:] if found_items else []
    
    recent_html = ""
    for item in recent_items:
        recent_html += f"""
        <div style="border: 1px solid #ddd; padding: 10px; margin: 5px 0; border-radius: 3px;">
            <strong>{item['title'][:50]}...</strong><br>
            Цена: {item['price']} руб. | {datetime.now().strftime('%H:%M:%S')}
        </div>
        """
    
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: Arial; margin: 20px; }}
            .status-box {{ background: #e9ecef; border: 1px solid #ddd; padding: 20px; border-radius: 5px; }}
        </style>
    </head>
    <body>
        <h2>📊 Статус системы</h2>
        <div class="status-box">
            <p><strong>Мониторинг:</strong> {status_text}</p>
            <p><strong>Всего товаров в памяти:</strong> {len(found_items)}</p>
            <p><strong>Время сервера:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
            <p><strong>Telegram бот:</strong> {'✅ Подключен' if TELEGRAM_BOT_TOKEN else '❌ Не подключен'}</p>
        </div>
        
        <h3>Последние товары ({len(recent_items)} из {len(found_items)}):</h3>
        {recent_html if recent_html else '<p>Нет данных</p>'}
        
        <p><a href="/">← Назад на главную</a></p>
    </body>
    </html>
    """

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook для Telegram бота (для обратной связи)"""
    try:
        data = request.get_json()
        
        if 'message' in data and 'text' in data['message']:
            text = data['message']['text']
            chat_id = data['message']['chat']['id']
            
            # Проверяем, что сообщение от нужного чата
            if str(chat_id) != TELEGRAM_CHAT_ID:
                return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
            
            if text == '/start':
                message = (
                    "🚀 <b>FunPay Hunter для Black Russia</b>\n\n"
                    "Я отслеживаю новые предложения по Black Russia на FunPay.\n\n"
                    "✅ <b>Только онлайн продавцы</b>\n"
                    "✅ <b>Фильтр по цене (10-50000 руб)</b>\n"
                    "✅ <b>Мгновенные уведомления</b>\n\n"
                    "📋 <b>Команды:</b>\n"
                    "/start - это сообщение\n"
                    "/check - проверить сейчас\n"
                    "/monitor - запустить мониторинг\n"
                    "/stop - остановить мониторинг\n"
                    "/status - статус системы\n"
                    "/help - помощь\n\n"
                    "🌐 <b>Веб-интерфейс:</b>\n"
                    "Откройте в браузере для управления:"
                )
                send_telegram_message(message)
            
            elif text == '/check':
                send_telegram_message("🔍 Проверяю новые предложения...")
                check_new_items()
                send_telegram_message(f"✅ Проверка завершена\nНайдено товаров: {len(found_items)}")
            
            elif text == '/monitor':
                global monitoring_active
                if not monitoring_active:
                    monitoring_active = True
                    thread = threading.Thread(target=monitoring_loop)
                    thread.daemon = True
                    thread.start()
                    send_telegram_message("✅ Мониторинг запущен!\nПроверка каждые 60 секунд.")
                else:
                    send_telegram_message("⚠️ Мониторинг уже запущен.")
            
            elif text == '/stop':
                monitoring_active = False
                send_telegram_message("⏸️ Мониторинг остановлен.")
            
            elif text == '/status':
                status = "🟢 АКТИВЕН" if monitoring_active else "🔴 ОСТАНОВЛЕН"
                message = (
                    f"📊 <b>Статус мониторинга</b>\n\n"
                    f"• Мониторинг: {status}\n"
                    f"• Отслеживаемых товаров: {len(found_items)}\n"
                    f"• Время: {datetime.now().strftime('%H:%M:%S')}\n\n"
                    f"🌐 <a href='https://black-russia-monitor.onrender.com/'>Веб-интерфейс</a>"
                )
                send_telegram_message(message)
            
            elif text == '/help':
                message = (
                    "❓ <b>Помощь</b>\n\n"
                    "Бот отслеживает новые предложения Black Russia на FunPay.\n\n"
                    "1. Нажмите /monitor для запуска автоматического мониторинга\n"
                    "2. Бот будет проверять каждые 60 секунд\n"
                    "3. При появлении нового товара вы получите уведомление\n"
                    "4. Отслеживаются только онлайн продавцы\n"
                    "5. Цена от 10 до 50000 руб\n\n"
                    "Проблемы? Перезапустите сервис на Render или напишите /start"
                )
                send_telegram_message(message)
        
        return jsonify({'status': 'ok'})
    
    except Exception as e:
        logger.error(f"❌ Ошибка в webhook: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health')
def health():
    """Проверка здоровья приложения"""
    return jsonify({
        'status': 'healthy',
        'monitoring': monitoring_active,
        'items_count': len(found_items),
        'timestamp': datetime.now().isoformat(),
        'version': '1.0'
    })

# Запуск приложения
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
