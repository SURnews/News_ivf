import feedparser
import telebot
import os
import re
import time
import hashlib
import urllib.parse
import psycopg2
from urllib.parse import urlparse as url_parse
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
import logging
from urllib.parse import parse_qs, urlunparse, urlencode
from googletrans import Translator
import deepl
import html
import sys
from datetime import datetime

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен и канал из переменных окружения
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHANNEL = os.getenv('TELEGRAM_CHANNEL')
DEEPL_API_KEY = os.getenv('DEEPL_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')  # Для PostgreSQL

# Проверка обязательных переменных
if not TOKEN or not CHANNEL or not DATABASE_URL:
    logger.error("Не заданы обязательные переменные окружения!")
    sys.exit(1)

# Инициализация переводчиков
translator = Translator()
deepl_translator = None
if DEEPL_API_KEY:
    try:
        deepl_translator = deepl.Translator(DEEPL_API_KEY)
    except Exception as e:
        logger.error(f"Ошибка инициализации DeepL: {e}")

# Глобальный список трекинговых параметров для удаления
TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'ref', 'referral', 'source', 'fbclid', 'gclid', 'yclid', '_openstat',
    'campaign', 'mc_cid', 'mc_eid', 'session_id', 'icid', 'trk', 'trk_contact',
    'utm_referrer', 'utm_reader', 'utm_place', 'fb_action_ids', 'fb_action_types',
    'fb_ref', 'ga_source', 'ga_medium', 'ga_term', 'ga_content', 'ga_campaign',
    'pk_source', 'pk_medium', 'pk_campaign'
}

# Подключение к базе данных PostgreSQL
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        logger.error(f"Ошибка подключения к базе данных: {e}")
        raise

# Создание таблицы при первом запуске
def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sent_news (
                normalized_link TEXT,
                original_link TEXT,
                title TEXT,
                title_hash TEXT,
                pubdate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (normalized_link, title_hash)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_title_hash 
            ON sent_news(title_hash)
        ''')
        conn.commit()
        logger.info("Таблица sent_news успешно создана или уже существует")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")
        # Попытка повторного создания таблицы
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_news (
                    normalized_link TEXT,
                    original_link TEXT,
                    title TEXT,
                    title_hash TEXT,
                    pubdate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (normalized_link, title_hash)
                )
            ''')
            conn.commit()
            logger.info("Таблица создана после повторной попытки")
        except Exception as e2:
            logger.error(f"Критическая ошибка при создании таблицы: {e2}")
            sys.exit(1)
    finally:
        if conn:
            conn.close()

# Инициализация базы при старте
init_db()

bot = telebot.TeleBot(TOKEN)

# Функция для вычисления хеша заголовка
def get_title_hash(title):
    """Вычисляет SHA-256 хеш для заголовка"""
    clean_title = title.strip().lower()
    return hashlib.sha256(clean_title.encode('utf-8')).hexdigest()

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

def normalize_url(url):
    """Улучшенная нормализация URL с удалением трекинговых параметров"""
    try:
        parsed = url_parse(url)
        
        # Приводим домен к нижнему регистру и удаляем www
        netloc = parsed.netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        
        # Удаляем якорь
        parsed = parsed._replace(fragment="", netloc=netloc)
        
        # Фильтрация параметров запроса
        if parsed.query:
            query_params = parse_qs(parsed.query, keep_blank_values=True)
            filtered_params = {}
            
            for key, values in query_params.items():
                # Приводим ключ к нижнему регистру для проверки
                key_lower = key.lower()
                
                # Сохраняем только нетрекинговые параметры
                if key_lower not in TRACKING_PARAMS:
                    filtered_params[key] = values
            
            # Сортируем параметры для единообразия
            sorted_params = sorted(filtered_params.items(), key=lambda x: x[0])
            new_query = urlencode(sorted_params, doseq=True)
            parsed = parsed._replace(query=new_query)
        
        # Убираем дублирующиеся слэши в пути
        path = re.sub(r'/{2,}', '/', parsed.path)
        parsed = parsed._replace(path=path)
        
        return urlunparse(parsed)
    except Exception as e:
        logger.error(f"Ошибка нормализации URL {url}: {e}")
        return url

def is_new(link, title):
    """Проверяет новость по нормализованному URL и хешу заголовка"""
    normalized = normalize_url(link)
    title_hash = get_title_hash(title)
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 1 FROM sent_news 
            WHERE normalized_link = %s AND title_hash = %s
        ''', (normalized, title_hash))
        return cursor.fetchone() is None
    except Exception as e:
        logger.error(f"Ошибка проверки новости: {e}")
        return True  # В случае ошибки считаем новость новой
    finally:
        if conn:
            conn.close()

def save_news(link, title):
    """Сохраняет новость с нормализованным URL и хешем заголовка"""
    try:
        normalized = normalize_url(link)
        title_hash = get_title_hash(title)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO sent_news 
            (normalized_link, original_link, title, title_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (normalized_link, title_hash) DO NOTHING
        ''', (normalized, link, title, title_hash))
        conn.commit()
        logger.info(f"Сохранена новость: {title}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении в базу: {e}")
    finally:
        if conn:
            conn.close()

def clean_html(raw_html):
    """Удаляет HTML-теги из текста"""
    if not raw_html:
        return ""
    cleanr = re.compile('<.*?>')
    cleantext = re.sub(cleanr, '', raw_html)
    return html.unescape(cleantext)

def contains_topic(content):
    """Проверяет, относится ли новость к нужной тематике с использованием регулярных выражений"""
    # Очищаем контент от HTML-тегов
    clean_content = clean_html(content).lower()
    
    # Основные ключевые слова на разных языках
    patterns = [
        # Русский
        r'\bсуррогатн\w*\b', r'\bэко\b', r'\bвспомогательные репродуктивные\b', 
        r'\bвпр\b', r'\bдонорство ооцитов\b', r'\bдонорство спермы\b',
        r'\bсурмама\b', r'\bсуррогатной матери\b', r'\bрепродуктивн\w*\b',
        r'\bбесплодие\b', r'\bоплодотворение in vitro\b',
        
        # Английский
        r'\bsurrogacy\b', r'\bivf\b', r'\bassisted reproductive technology\b', 
        r'\bart\b', r'\begg donation\b', r'\bsperm donation\b',
        r'\bfertility treatment\b', r'\bin vitro fertilization\b',
        r'\bembryo transfer\b', r'\bsurrogate mother\b', r'\bfertility clinic\b',
        r'\breproductive medicine\b', r'\bgestational carrier\b',
        
        # Испанский
        r'\bmaternidad subrogada\b', r'\bfiv\b', r'\bdonación de óvulos\b', 
        r'\bdonación de esperma\b', r'\bgestación subrogada\b',
        r'\bfertilidad\b', r'\breproducción asistida\b',
        
        # Французский
        r'\bmère porteuse\b', r'\bpma\b', r'\bdon d’ovocytes\b', 
        r'\bdon de sperme\b', r'\bprocréation médicalement assistée\b',
        r'\bgestation pour autrui\b', r'\bfertilité\b',
        
        # Немецкий
        r'\bleihmutterschaft\b', r'\bkünstliche befruchtung\b', r'\beizellspende\b',
        r'\bsamenspende\b', r'\breproduktionsmedizin\b', r'\bfruchtbarkeit\b',
        r'\bin-vitro-fertilisation\b',
        
        # Итальянский
        r'\bmaternità surrogata\b', r'\bgravidanza surrogata\b', r'\bdonazione di ovociti\b',
        r'\bdonazione di sperma\b', r'\bprocreazione medicalmente assistita\b',
        r'\bfertilita\b', r'\bfivet\b',
        
        # Китайский
        r'\b代孕\b', r'\b试管婴儿\b', r'\b卵子捐赠\b', r'\b精子捐赠\b',  # Иероглифы
        r'\bdàiyùn\b', r'\bshìguǎn yīngér\b', r'\bluǎnzǐ juānzèng\b',  # Пиньинь
        r'\bjīngzǐ juānzèng\b', r'\b辅助生殖\b'
    ]
    
    # Проверка по всем паттернам
    for pattern in patterns:
        if re.search(pattern, clean_content, re.IGNORECASE):
            return True
    
    # Дополнительная проверка для аббревиатур
    if re.search(r'\bart\b', clean_content, re.IGNORECASE):
        # Проверка контекста для ART (чтобы исключить "искусство")
        context_keywords = [
            'репродуктивн', 'fertility', 'fiv', 'оплодотворен', 'reproduction',
            'reproducción', 'reprodução', 'fortplantning', 'воспроизведение'
        ]
        if any(kw in clean_content for kw in context_keywords):
            return True
    
    return False

# Расширенный список международных RSS-лент
RSS_FEEDS = [
    # Русскоязычные
    'https://lenta.ru/rss/news',
    'https://rssexport.rbc.ru/rbcnews/news/30/full.rss',
    'https://tass.ru/rss/v2.xml',
    'https://rssexport.rbc.ru/rbcnews/news/30/full.rss',
    
    # Англоязычные
    'https://www.fertilitynetworkuk.org/feed/',
    'https://www.news-medical.net/tag/feed/ivf.aspx',
    'https://www.technologyreview.com/feed/',
    'https://www.sciencedaily.com/rss/health_medicine/fertility.xml',
    'https://www.nih.gov/news-events/news-releases/rss.xml',
    'https://www.bbc.com/news/health/rss.xml',
    'https://www.reutersagency.com/feed/?best-topics=health',
    'http://rss.cnn.com/rss/edition_health.rss',
    'https://www.theguardian.com/science/health/rss',
    'https://rss.dw.com/rdf/rss-en-health',
    
    # Французские
    'https://www.lemonde.fr/sante/rss_full.xml',
    'https://www.lefigaro.fr/rss/figaro_sante.xml',
    'https://www.liberation.fr/arc/outboundfeeds/rss/section/sante/?outputType=xml',
    
    # Испанские
    'https://www.elmundo.es/rss/salud.xml',
    'https://www.abc.es/rss/feeds/abc_Salud.xml',
    'https://elpais.com/sociedad/salud/rss',
    
    # Немецкие
    'https://www.spiegel.de/gesundheit/index.rss',
    'https://www.faz.net/rss/aktuell/gesundheit/',
    'https://www.sueddeutsche.de/gesundheit/rss',
    
    # Итальянские
    'https://www.corriere.it/salute/rss',
    'https://www.repubblica.it/salute/rss',
    'https://www.lastampa.it/rss/salute',
    
    # Китайские
    'http://www.xinhuanet.com/english/rss/healthrss.xml',
    'https://www.chinanews.com/rss/health.shtml',
    'https://www.scmp.com/rss/90/feed',  # Health section
    
    # Японские
    'https://www.nikkei.com/rss/news/cate/health.html',
    
    # Корейские
    'https://www.koreabiomed.com/rss/news',
    
    # Португальские (Бразилия)
    'https://g1.globo.com/rss/g1/saude/',
    
    # Арабские
    'https://www.aljazeera.net/xml/rss/all.xml',
    
    # Индийские
    'https://www.thehindu.com/sci-tech/health/rss'
]

def check_feeds():
    start_time = time.time()
    logger.info('Начало проверки новостей...')
    new_count = 0
    duplicate_count = 0
    irrelevant_count = 0
    
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            if not feed.entries:
                logger.warning(f"Нет новостей в фиде: {url}")
                continue
                
            logger.info(f"Обработка фида: {url} ({len(feed.entries)} новостей)")
            
            for entry in feed.entries:
                try:
                    # Проверяем время выполнения
                    if time.time() - start_time > 240:  # 4 минуты
                        logger.warning("Прерывание проверки из-за превышения времени")
                        break
                        
                    link = entry.get('link', '')
                    title = clean_html(entry.get('title', ''))
                    summary = clean_html(entry.get('summary', entry.get('description', '')))
                    
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
                    if is_new(link, title):
                        # Задержка для избежания блокировки
                        time.sleep(0.2)
                        
                        send_news(title, link)
                        save_news(link, title)
                        new_count += 1
                    else:
                        duplicate_count += 1
                        logger.info(f"Дубликат новости: {title} | URL: {normalize_url(link)}")
                except Exception as e:
                    logger.error(f"Ошибка при обработке новости: {e}")
        
        except Exception as e:
            logger.error(f"Ошибка при обработке RSS {url}: {e}")
            time.sleep(2)  # Задержка при ошибках
    
    # Очистка старых записей
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sent_news WHERE pubdate < NOW() - INTERVAL '30 days'")
        deleted_count = cursor.rowcount
        conn.commit()
        logger.info(f"Очищено старых записей: {deleted_count}")
    except Exception as e:
        logger.error(f"Ошибка очистки БД: {e}")
    finally:
        if conn:
            conn.close()
    
    elapsed = time.time() - start_time
    logger.info(f"Проверка завершена за {elapsed:.2f} сек. Новые: {new_count}, дубликаты: {duplicate_count}, не по теме: {irrelevant_count}")

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
        
        # Форматирование сообщения с гиперссылкой вместо полного URL
        message = f"🔬 *{title}*\n\n[Источник]({link})\n\n"
        
        # Добавляем хэштеги в зависимости от языка
        hashtags = "#ВРТ #ЭКО #СуррогатноеМатеринство"
        if translated:
            hashtags += " #Перевод"
        
        message += hashtags
        
        # Отправляем сообщение с поддержкой Markdown
        bot.send_message(
            CHANNEL, 
            message, 
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        logger.info(f"Отправлена новость: {original_title}")
    except Exception as e:
        logger.error(f'Ошибка отправки: {e}')

# Планировщик с интервалом 5 минут
scheduler = BackgroundScheduler()
scheduler.add_job(
    check_feeds,
    'interval',
    minutes=5,
    max_instances=1,
    next_run_time=datetime.now()  # Запустить сразу при старте
)
scheduler.start()

# Flask приложение
app = Flask(__name__)

@app.route('/')
def home():
    return "Бот активен! Следующая проверка новостей через 5 минут"

@app.route('/check-now')
def manual_check():
    """Ручной запуск проверки"""
    try:
        check_feeds()
        return "Проверка новостей запущена!"
    except Exception as e:
        return f"Ошибка: {str(e)}", 500

@app.route('/health')
def health_check():
    return "OK", 200

if __name__ == '__main__':
    # Запускаем Flask
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Запуск Flask приложения на порту {port}")
    app.run(host='0.0.0.0', port=port)
