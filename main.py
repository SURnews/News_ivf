import feedparser
import telebot
import sqlite3
import os
import re
import time
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
from urllib.parse import urlparse
from googletrans import Translator
import deepl

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройки
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL = os.getenv('TELEGRAM_CHANNEL')
DEEPL_API_KEY = os.getenv('DEEPL_API_KEY')

translator = Translator()
deepl_translator = None

if DEEPL_API_KEY:
    try:
        deepl_translator = deepl.Translator(DEEPL_API_KEY)
    except Exception as e:
        logger.error(f"Ошибка инициализации DeepL: {e}")

bot = telebot.TeleBot(TOKEN)

# База данных
conn = sqlite3.connect('news.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS sent_news (
        normalized_link TEXT PRIMARY KEY,
        original_link TEXT,
        title TEXT,
        pubdate TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')
conn.commit()

# Перевод
def translate_text(text, src='auto', dest='ru'):
    try:
        if deepl_translator:
            result = deepl_translator.translate_text(text, target_lang=dest.upper())
            return result.text
        result = translator.translate(text, src=src, dest=dest)
        return result.text
    except Exception as e:
        logger.error(f"Ошибка перевода: {e}")
        return text

# Проверка уникальности
def normalize_url(url):
    try:
        parsed = urlparse(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return clean_url.split('#')[0]
    except Exception as e:
        logger.error(f"Ошибка нормализации URL {url}: {e}")
        return url

def is_new(link):
    normalized = normalize_url(link)
    cursor.execute('SELECT 1 FROM sent_news WHERE normalized_link = ?', (normalized,))
    return cursor.fetchone() is None

def save_news(link, title):
    try:
        normalized = normalize_url(link)
        cursor.execute('''
            INSERT OR IGNORE INTO sent_news (normalized_link, original_link, title)
            VALUES (?, ?, ?)
        ''', (normalized, link, title))
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения в БД: {e}")

# Тематическая фильтрация
def contains_topic(content):
    patterns = [
        r'\bсуррогат\w*\b', r'\bэко\b', r'\bвспомогательные репродуктивные\b',
        r'\bivf\b', r'\bsurrogacy\b', r'\bfertility\b',
        r'\bматеринство\b', r'\bрепродуктивн\w*\b',
        r'\bdonor\b', r'\begg donation\b', r'\bsperm donation\b',
        r'\bassisted reproductive\b', r'\bart\b', r'\b代孕\b', r'\b试管婴儿\b',
    ]
    text = content.lower()
    return any(re.search(pattern, text) for pattern in patterns)

# Список RSS
RSS_FEEDS = [
    'https://www.ivfmedia.ru/rss',
    'https://reprobank.ru/about/media/rss/',
    'https://www.ivf.ru/feed/',
    'https://altravita-ivf.ru/blog/rss/',
    'https://www.fertilitynetworkuk.org/feed/',
    'https://www.fertstert.org/rss',
    'https://www.fertilityscience.org/feed',
    'https://www.ivf.net/rss',
    'https://www.news-medical.net/tag/feed/ivf.aspx',
    'https://www.nih.gov/news-events/news-releases/rss.xml',
    'https://www.eurekalert.org/rss/medicine.xml',
    'https://www.who.int/rss-feeds/news-articles/en/',
    'https://feeds.bbci.co.uk/news/world/rss.xml',
    'https://www.reutersagency.com/feed/?best-topics=world',
    'https://www.aljazeera.com/xml/rss/all.xml'
]

# Отправка новости
def send_news(title, link):
    try:
        detected_lang = translator.detect(title).lang
        translated = False
        if detected_lang != 'ru':
            title_ru = translate_text(title, src=detected_lang, dest='ru')
            if title_ru != title:
                title = f"{title_ru}\n({title})"
                translated = True

        message = f"📰 *{title}*\n\n{link}\n\n#ВРТ #ЭКО #СуррогатноеМатеринство"
        if translated:
            message += " #Перевод"

        bot.send_message(CHANNEL, message, parse_mode='Markdown')
        logger.info(f"Отправлена новость: {title}")
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# Проверка RSS
def check_feeds():
    logger.info("Проверка RSS...")
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                link = entry.get('link', '')
                title = entry.get('title', '')
                summary = entry.get('summary', '')

                if not link or not title:
                    continue

                full_text = f"{title} {summary}"

                if not contains_topic(full_text):
                    continue

                if is_new(link):
                    send_news(title, link)
                    save_news(link, title)
                    time.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка обработки {url}: {e}")

# Планировщик
scheduler = BackgroundScheduler()
scheduler.add_job(check_feeds, 'interval', hours=1)
scheduler.start()

# Flask-приложение
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот активен! Проверяет новости каждый час."

@app.route('/check-now')
def manual_check():
    try:
        check_feeds()
        return "Проверка запущена!"
    except Exception as e:
        return f"Ошибка запуска: {e}", 500

if __name__ == '__main__':
    check_feeds()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


