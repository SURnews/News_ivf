import feedparser
import telebot
import sqlite3
import os
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
import logging

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен и канал из переменных окружения
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL = os.getenv('TELEGRAM_CHANNEL')

if not TOKEN or not CHANNEL:
    logger.error("Не заданы переменные окружения TELEGRAM_TOKEN или TELEGRAM_CHANNEL")
    exit(1)

bot = telebot.TeleBot(TOKEN)

# База для отправленных новостей
conn = sqlite3.connect('news.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS sent_news (link TEXT PRIMARY KEY)')
conn.commit()

def send_news(title, link):
    try:
        bot.send_message(CHANNEL, f'📰 {title}\n{link}')
        logger.info(f"Отправлена новость: {title}")
    except Exception as e:
        logger.error(f'Ошибка отправки: {e}')

def is_new(link):
    cursor.execute('SELECT 1 FROM sent_news WHERE link = ?', (link,))
    return cursor.fetchone() is None

def save_news(link):
    try:
        cursor.execute('INSERT OR IGNORE INTO sent_news (link) VALUES (?)', (link,))
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка при сохранении в базу: {e}")

# RSS-ленты и ключевые слова
RSS_FEEDS = [
    'https://lenta.ru/rss',
    'https://tass.ru/rss/v2.xml',
    'https://rss.cnn.com/rss/edition.rss',
    'https://elpais.com/rss/feed.html?feedId=1022',
    'https://www.lemonde.fr/rss/une.xml',
    'https://www.dw.com/de/top-thema/s-9090/rss',
]

KEYWORDS = [
    'суррогатное материнство', 'ЭКО', 'ВРТ', 'донорство ооцитов', 'донорство спермы',
    'surrogacy', 'IVF', 'ART', 'egg donation', 'sperm donation',
    'gestación subrogada', 'donación de óvulos', 'donación de esperma',
    'mère porteuse', 'PMA', 'don d’ovocytes', 'don de sperme',
    '代孕', '试管婴儿', '卵子捐赠', '精子捐赠',
]

def check_feeds():
    logger.info('Проверка новостей...')
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                content = f"{entry.title} {getattr(entry, 'summary', '')}".lower()
                link = entry.get('link', '')
                
                if not link:
                    continue
                
                if any(kw.lower() in content for kw in KEYWORDS) and is_new(link):
                    send_news(entry.title, link)
                    save_news(link)
        except Exception as e:
            logger.error(f"Ошибка при обработке RSS {url}: {e}")

# Планировщик
scheduler = BackgroundScheduler()
scheduler.add_job(check_feeds, 'interval', hours=1)
scheduler.start()

# Flask приложение
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

@app.route('/webhook', methods=['POST'])
def webhook():
    """Endpoint для обработки вебхуков (если понадобится в будущем)"""
    return "OK", 200

if __name__ == '__main__':
    # Первая проверка при запуске
    check_feeds()
    
    # Запускаем Flask
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
