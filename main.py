import feedparser
import telebot
import sqlite3
import os
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread

# Токен и канал подтягиваются из переменных окружения
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL = os.getenv('TELEGRAM_CHANNEL')

bot = telebot.TeleBot(TOKEN) 


# База для уже отправленных новостей
conn = sqlite3.connect('news.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS sent_news (link TEXT PRIMARY KEY)')
conn.commit()

def send_news(title, link):
    try:
        bot.send_message(CHANNEL, f'📰 {title}\n{link}')
    except Exception as e:
        print('Ошибка отправки:', e)

def is_new(link):
    cursor.execute('SELECT 1 FROM sent_news WHERE link = ?', (link,))
    return cursor.fetchone() is None

def save_news(link):
    cursor.execute('INSERT INTO sent_news (link) VALUES (?)', (link,))
    conn.commit()

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
    print('Проверка новостей...')
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            if any(kw.lower() in entry.title.lower() for kw in KEYWORDS):
                if is_new(entry.link):
                    send_news(entry.title, entry.link)
                    save_news(entry.link)

# Планировщик запускает парсинг раз в час
scheduler = BackgroundScheduler()
scheduler.add_job(check_feeds, 'interval', hours=1)
scheduler.start()

@bot.message_handler(func=lambda message: True)
def catch_all(message):
    print(f"Поймано сообщение! Chat ID: {message.chat.id}")

# Flask-сервер для Render + UptimeRobot
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

# Запускаем Flask в отдельном потоке
flask_thread = Thread(target=run_flask)
flask_thread.start()

print('Бот запущен и слушает...')

# Бот сам ничего не делает в цикле — только Flask держит Render активным
