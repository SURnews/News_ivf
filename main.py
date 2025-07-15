import feedparser
import telebot
import sqlite3
import os
import re
import time
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
import logging
from urllib.parse import urlparse
from googletrans import Translator
import deepl

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен и канал из переменных окружения
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL = os.getenv('TELEGRAM_CHANNEL')
DEEPL_API_KEY = os.getenv('DEEPL_API_KEY')  # Опционально

# Инициализация переводчиков
translator = Translator()
deepl_translator = None
if DEEPL_API_KEY:
    try:
        deepl_translator = deepl.Translator(DEEPL_API_KEY)
    except Exception as e:
        logger.error(f"Ошибка инициализации DeepL: {e}")

def translate_text(text, src='auto', dest='ru'):
    """Переводит текст на русский язык с использованием доступных сервисов"""
    if not text.strip():
        return text
        
    try:
        # Пробуем DeepL если доступен
        if deepl_translator:
            result = deepl_translator.translate_text(text, target_lang=dest)
            return result.text
        
        # Используем Google Translate как резервный вариант
        translation = translator.translate(text, src=src, dest=dest)
        return translation.text
    except Exception as e:
        logger.error(f"Ошибка перевода: {e}")
        return text  # Возвращаем оригинальный текст в случае ошибки

bot = telebot.TeleBot(TOKEN)

# База для отправленных новостей
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
cursor.execute('CREATE INDEX IF NOT EXISTS idx_normalized_link ON sent_news(normalized_link)')
conn.commit()

def normalize_url(url):
    """Удаляет UTM-метки и другие ненужные параметры из URL"""
    try:
        parsed = urlparse(url)
        # Оставляем только схему, домен и путь
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # Удаляем якоря
        return clean_url.split('#')[0]
    except Exception as e:
        logger.error(f"Ошибка нормализации URL {url}: {e}")
        return url

def is_new(link):
    """Проверяет новость по нормализованному URL"""
    normalized = normalize_url(link)
    cursor.execute('SELECT 1 FROM sent_news WHERE normalized_link = ?', (normalized,))
    return cursor.fetchone() is None

def save_news(link, title):
    """Сохраняет новость с нормализованным URL"""
    try:
        normalized = normalize_url(link)
        cursor.execute('''
            INSERT OR IGNORE INTO sent_news (normalized_link, original_link, title)
            VALUES (?, ?, ?)
        ''', (normalized, link, title))
        conn.commit()
        logger.info(f"Сохранена новость: {title}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении в базу: {e}")

def contains_topic(content):
    """Проверяет, относится ли новость к нужной тематике с использованием регулярных выражений"""
    # Основные ключевые слова на разных языках
    patterns = [
        # Русский
        r'\bсуррогатн\w*\b', r'\bэко\b', r'\bвспомогательные репродуктивные\b', 
        r'\bвпр\b', r'\bдонорство ооцитов\b', r'\bдонорство спермы\b',
        r'\bсурмама\b', r'\bсуррогатной матери\b', r'\bрепродуктивн\w*\b',
        
        # Английский
        r'\bsurrogacy\b', r'\bivf\b', r'\bassisted reproductive technology\b', 
        r'\bart\b', r'\begg donation\b', r'\bsperm donation\b',
        r'\bfertility treatment\b', r'\bin vitro fertilization\b',
        r'\bembryo transfer\b', r'\bsurrogate mother\b',
        
        # Испанский
        r'\bmaternidad subrogada\b', r'\bfiv\b', r'\bdonación de óvulos\b', 
        r'\bdonación de esperma\b', r'\bgestación subrogada\b',
        
        # Французский
        r'\bmère porteuse\b', r'\bpma\b', r'\bdon d’ovocytes\b', 
        r'\bdon de sperme\b', r'\bprocréation médicalement assistée\b',
        
        # Немецкий
        r'\bleihmutterschaft\b', r'\bkünstliche befruchtung\b', r'\beizellspende\b',
        r'\bsamenspende\b', r'\breproduktionsmedizin\b',
        
        # Итальянский
        r'\bmaternità surrogata\b', r'\bgravidanza surrogata\b', r'\bdonazione di ovociti\b',
        r'\bdonazione di sperma\b', r'\bprocreazione medicalmente assistita\b',
        
        # Китайский (транслитерация)
        r'\bdàiyùn\b', r'\bshìguǎn yīng'ér\b', r'\bluǎnzǐ juānzèng\b',
        r'\bjīngzǐ juānzèng\b', r'\b试管婴儿\b', r'\b代孕\b', r'\b卵子捐赠\b', r'\b精子捐赠\b'
    ]
    
    content_lower = content.lower()
    
    # Проверка по всем паттернам
    for pattern in patterns:
        if re.search(pattern, content_lower, re.IGNORECASE):
            return True
    
    # Дополнительная проверка для аббревиатур
    if re.search(r'\bart\b', content_lower, re.IGNORECASE):
        # Проверка контекста для ART (чтобы исключить "искусство")
        context_keywords = ['репродуктивн', 'fertility', 'fiv', 'оплодотворен', 'reproduction']
        if any(kw in content_lower for kw in context_keywords):
            return True
    
    return False

# Расширенный список RSS-лент
RSS_FEEDS = [
    # Русскоязычные
    'https://www.ivfmedia.ru/rss',
    'https://reprobank.ru/about/media/rss/',
    'https://www.ivf.ru/feed/',
    'https://altravita-ivf.ru/blog/rss/',
     'https://lenta.ru/rss',
    'https://tass.ru/rss/v2.xml',
    'https://rss.cnn.com/rss/edition.rss',
  
    
    # Англоязычные
    'https://www.fertilitynetworkuk.org/feed/',
    'https://www.fertstert.org/rss',
    'https://www.fertilityscience.org/feed',
    'https://www.ivf.net/rss',
    'https://www.news-medical.net/tag/feed/ivf.aspx',
    'https://www.technologyreview.com/feed/',
    'https://people.com/feed/',
    'https://www.scmp.com/rss/91/feed',  # South China Morning Post
    'https://www.chinadaily.com.cn/rss/cndy_rss.xml',
    'https://elpais.com/rss/feed.html?feedId=1022',
    'https://www.lemonde.fr/rss/une.xml',
    'https://feeds.bbci.co.uk/news/world/rss.xml',
    'https://www.reutersagency.com/feed/?best-topics=world',
    'http://rss.cnn.com/rss/edition.rss',
    'https://www.theguardian.com/world/rss',
    'https://rss.dw.com/rdf/rss-en-all',
    'https://www.aljazeera.com/xml/rss/all.xml'
    'https://www.medscape.com/rss/public/obgyn',
    'https://www.news-medical.net/tag/feed/Reproductive-Health.aspx',
    'https://www.sciencedaily.com/rss/health_medicine/fertility.xml',
    'https://www.nih.gov/news-events/news-releases/rss.xml',
    'https://www.eurekalert.org/rss/medicine.xml',
    'https://www.who.int/rss-feeds/news-articles/en/'

    # Испанские
    'https://www.infosalus.com/rss/actualidad.xml',
    'https://www.redaccionmedica.com/rss/noticias',
    'https://www.consalud.es/rss/actualidad',
    
    # Итальянские
    'https://www.ogginotizie.it/rss/salute.xml',
    'https://www.quotidianosanita.it/rss.php',
    'https://www.repubblica.it/salute/rss',
    
    # Немецкие
    'https://www.aerzteblatt.de/rss.xml',
    'https://www.baby-und-familie.de/rss.xml',
    'https://www.kinderaerzte-im-netz.de/rss.xml',
    'https://www.dw.com/de/top-thema/s-9090/rss',
    
    # Китайские (англоязычные)
    'https://www.chinadaily.com.cn/rss/cndy_rss.xml',
    'https://www.scmp.com/rss/91/feed',
    'https://www.globaltimes.cn/rss/health.xml'
]

def check_feeds():
    logger.info('Начало проверки новостей...')
    new_count = 0
    duplicate_count = 0
    irrelevant_count = 0
    
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            logger.info(f"Обработка фида: {url} ({len(feed.entries)} новостей)")
            
            for entry in feed.entries:
                link = entry.get('link', '')
                title = entry.get('title', '')
                summary = entry.get('summary', '')
                
                if not link or not title:
                    continue
                
                # Полный текст для анализа
                full_content = f"{title} {summary}".lower()
                
                # Усиленная проверка тематики
                if not contains_topic(full_content):
                    irrelevant_count += 1
                    logger.debug(f"Новость не по теме: {title}")
                    continue
                
                # Проверка уникальности
                if is_new(link):
                    # Задержка для избежания блокировки
                    time.sleep(1)
                    
                    send_news(title, link)
                    save_news(link, title)
                    new_count += 1
                else:
                    duplicate_count += 1
                    logger.info(f"Дубликат новости: {title}")
        
        except Exception as e:
            logger.error(f"Ошибка при обработке RSS {url}: {e}")
    
    logger.info(f"Проверка завершена. Новые: {new_count}, дубликаты: {duplicate_count}, не по теме: {irrelevant_count}")

def send_news(title, link):
    try:
        original_title = title
        translated = False
        
        # Пытаемся определить язык
        try:
            detected_lang = translator.detect(title).lang
            if detected_lang != 'ru':
                # Переводим только если язык определен и не русский
                ru_title = translate_text(title, src=detected_lang, dest='ru')
                if ru_title != title:
                    title = f"{ru_title}\n({original_title})"
                    translated = True
        except Exception as e:
            logger.error(f"Ошибка определения языка: {e}")
        
        # Форматирование сообщения с хэштегами
        message = f"🔬 *{title}*\n\n{link}\n\n"
        
        # Добавляем хэштеги в зависимости от языка
        hashtags = "#ВРТ #ЭКО #СуррогатноеМатеринство"
        if translated:
            hashtags += " #Перевод"
        
        message += hashtags
        
        bot.send_message(CHANNEL, message, parse_mode='Markdown')
        logger.info(f"Отправлена новость: {original_title}")
    except Exception as e:
        logger.error(f'Ошибка отправки: {e}')

# Планировщик
scheduler = BackgroundScheduler()
scheduler.add_job(check_feeds, 'interval', hours=1)
scheduler.start()

# Flask приложение
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот активен! Следующая проверка новостей через 1 час"

@app.route('/check-now')
def manual_check():
    """Ручной запуск проверки"""
    try:
        check_feeds()
        return "Проверка новостей запущена!"
    except Exception as e:
        return f"Ошибка: {str(e)}", 500

if __name__ == '__main__':
    # Первая проверка при запуске
    check_feeds()
    
    # Запускаем Flask
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
