# ivf_news_aggregator.py
import sys
import importlib.util
import os
import logging
import logging.handlers
import hashlib
import time
import json
import re
import asyncio
import feedparser
import httpx
import numpy as np
from datetime import datetime, timedelta
from sqlalchemy import create_engine, Column, String, Text, JSON, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.exc import SQLAlchemyError
from contextlib import contextmanager
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.error import TelegramError, RetryAfter
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

import tracemalloc
tracemalloc.start(25)


# Патч для модуля cgi в Python 3.13+
if sys.version_info >= (3, 13):
    cgi_spec = importlib.util.spec_from_loader('cgi', loader=None)
    cgi_module = importlib.util.module_from_spec(cgi_spec)
    sys.modules['cgi'] = cgi_module

    def escape(s, quote=True):
        replacements = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;' if quote else '"',
            "'": '&#x27;' if quote else "'"
        }
        return ''.join(replacements.get(c, c) for c in s)

    cgi_module.escape = escape

from dotenv import load_dotenv

# === ЗАГРУЗКА ОКРУЖЕНИЯ ===
load_dotenv()

# 🎯 ОТДЕЛЬНЫЙ ЛОГГЕР ДЛЯ БД - ДОБАВИТЬ ИМЕННО ЗДЕСЬ:
db_logger = logging.getLogger('database')
db_logger.setLevel(logging.INFO)

# Настройка отдельного форматирования для БД
db_handler = logging.StreamHandler()
db_handler.setFormatter(logging.Formatter(
    '%(asctime)s - 🗄️ [DATABASE] - %(levelname)s - %(message)s'
))
db_logger.addHandler(db_handler)
db_logger.propagate = False  # Не дублировать в основном логе


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("aggregator.log"),
        logging.handlers.RotatingFileHandler(  
            "aggregator.log", maxBytes=10*1024*1024, backupCount=5
        )
    ]
)
logger = logging.getLogger("IVFNewsAggregator")

# Конфигурация
class Config:
    # Режим работы AI: 'local' или 'api'
    AI_MODE = os.getenv("AI_MODE", "local").lower()
    AI_API_URL = os.getenv("AI_API_URL")
    AI_MODEL = os.getenv("AI_MODEL")
    AI_API_KEY = os.getenv("AI_API_KEY")
    AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", 0.7))
    AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", 350))
    
    # Настройки базы данных
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")
    @classmethod  
    def get_database_url(cls):
        """Получить правильно отформатированный URL для SQLAlchemy"""
        database_url = cls.DATABASE_URL
        if not database_url:
            raise RuntimeError("DATABASE_URL не установлен в переменных окружения")

        # Конвертация для SQLAlchemy с psycopg2
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif database_url.startswith("postgresql://") and "psycopg2" not in database_url:
            database_url = database_url.replace("postgresql://", "postgresql+psycopg2://", 1)

        return database_url

    # Настройки Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "@Futurefamilylab")
    
    # Параметры сбора новостей
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 1800))  # 30 минут
    MAX_TEXT_LENGTH = 4096
    EMBEDDING_UPDATE_INTERVAL = 1800  # 30 минут
    MAX_NEWS_PER_CYCLE = 10  # Ограничение новых новостей за цикл
    
    # Обновленные рабочие RSS-фиды
    RSS_FEEDS = [
        "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
        "https://news.search.yahoo.com/search;_ylt=Awrhbcnaco5oAwIAl.ZXNyoA;_ylu=Y29sbwNiZjEEcG9zAzEEdnRpZAMEc2VjA3Nj?p=surrogacy&fr=yfp-t&fr2=p%3As%2Cv%3Aw%2Cm%3Anewsdd_bn_m%2Cct%3Abing",
        "https://www.fertilitynetworkuk.org/feed/",
        "https://www.news-medical.net/tag/feed/ivf.aspx",
        "https://www.technologyreview.com/feed/",
        "https://www.sciencedaily.com/rss/health_medicine/fertility.xml",
        "https://www.medicalnewstoday.com/categories/fertility/rss",
        "https://www.straitstimes.com/news/health/rss",
        "https://www.eurekalert.org/news/rss/health.xml",
        "https://www.nature.com/subjects/health-care.rss",
        "https://www.lemonde.fr/sante/rss_full.xml",
        "https://www.spiegel.de/gesundheit/index.rss",
        "https://www.repubblica.it/salute/rss",
        "https://www.japantimes.co.jp/feed/tag/health/",
        "http://www.xinhuanet.com/english/rss/healthrss.xml",
        "https://news.google.com/search?q=%22IVF%7CFertility%7CEmbryo%7CPGT%7CSurrogacy%7CSpermDonation%7CEggDonation%7CFertilityLaw%22&hl=en-US&gl=US&ceid=US%3Aen",  
        "https://news.google.com/rss/search?q=IVF%7CFertility%7CEmbryo%7CPGT%7CSurrogacy%7CSpermDonation%7CEggDonation%7CFertilityLaw&hl=ru-RU&gl=US&ceid=US:ru",
        'https://www.lefigaro.fr/rss/figaro_sante.xml',
        'https://news.google.com/rss/search?q=IVF%7CFertility%7CEmbryo%7CPGT%7CSurrogacy%7CSpermDonation%7CEggDonation%7CFertilityLaw&hl=ru-RU&gl=US&ceid=US:ru',
        'https://www.elmundo.es/rss/salud.xml',
        'https://xml2.corriereobjects.it/rss/salute.xml',
        'https://www.lastampa.it/rss/salute',
        'https://www.scmp.com/rss/4/feed',
        'https://www.abc.es/rss/feeds/abc_Salud.xml',
        'https://elpais.com/salud-y-bienestar/rss/',
        'https://www.faz.net/aktuell/gesundheit/rss.xml',
        'https://www.liberation.fr/sante/rss',
        'https://news.google.com/rss/search?q=IVF+Fertility+Embryo+PGT+Surrogacy+SpermDonation+EggDonation+FertilityLaw&hl=ru&gl=RU&ceid=RU:ru',
        'https://ccrmivf.com/feed',
        'https://doctoreko.ru/news/rss',
        'https://www.sciencedaily.com/rss/health_medicine/fertility.xml',
        'https://www.sciencedaily.com/rss/health_medicine/gynecology.xml', 
        'https://www.sciencedaily.com/rss/health_medicine/pregnancy_and_childbirth.xml', 
        'https://www.nature.com/subjects/embryology.rss',
        'https://www.sciencedaily.com/rss/plants_animals/genetics.xml', 
        'https://www.sciencedaily.com/rss/health_medicine/genes.xml',
        'https://www.sciencedaily.com/rss/health_medicine/stem_cells.xml',
        'https://www.sciencedaily.com/rss/plants_animals/developmental_biology.xml', 
        'https://www.sciencedaily.com/rss/health_medicine/epigenetics.xml',
        'https://www.sciencedaily.com/rss/health_medicine/birth_defects.xml',
        'https://www.sciencedaily.com/rss/health_medicine/gene_therapy.xml',
        'https://www.sciencedaily.com/rss/plants_animals/cloning.xml',
        'https://elementy.ru/rss/news',
        'http://rss.sciam.com/basic-science', 
        'https://www.sciencedaily.com/rss/all.xml', 
        'https://www.sciencedaily.com/rss/top/health.xml',  
        'https://www.sciencedaily.com/rss/top/science.xml', 
        'https://www.theguardian.com/science/rss',
        'https://www.technologyreview.com/feed/',
        'https://www.sciencedaily.com/rss/science_society/bioethics.xml',
        'https://www.theguardian.com/rss',

    ]
    
    # Ключевые слова для фильтрации новостей
    KEYWORDS = [
        'ivf', 'fertility', 'эко', 'фертильность', 'embryo', 'эмбрион', 'egg donation',
        'in vitro', 'искусственное оплодотворение', 'репродукция', 'surrogacy',
        'infertility', 'бесплодие', 'egg freezing', 'криоконсервация',
        'sperm donation', 'донорство спермы', 'embryo transfer', 'перенос эмбрионов',
        'fertility treatment', 'лечение бесплодия', 'assisted reproduction',
        'вспомогательные репродуктивные технологии', 'плод', 'зачатие'
    ]

    # Модели для локального ИИ
    LOCAL_AI_MODELS = {
        "embedding": "sentence-transformers/all-MiniLM-L6-v2",
        "summarization": "IlyaGusev/rut5_base_sum_gazeta",
        "translation": "Helsinki-NLP/opus-mt-en-ru"
    }
    
    # Медицинский глоссарий (исправлено: добавлена запятая после "гестация")
    MEDICAL_GLOSSARY = {
        "ivf": "ЭКО",
        "in vitro fertilization": "экстракорпоральное оплодотворение",
        "embryo": "эмбрион",
        "implantation": "имплантация",
        "fertility": "фертильность",
        "blastocyst": "бластоциста",
        "ovarian stimulation": "стимуляция яичников",
        "sperm": "сперматозоид",
        "oocyte": "ооцит",
        "zygote": "зигота",
        "gestation": "гестация",  # Исправлено: добавлена запятая
        "PGT": "ПГТ (преимплантационное генетическое тестирование)",
        "ICSI": "ИКСИ (интрацитоплазматическая инъекция сперматозоида)",
        "ovulation induction": "индукция овуляции",
        "endometrium": "эндометрий",
        "follicle": "фолликул",
        "gamete": "гамета",
        "zygote intrafallopian transfer": "зиготный внутрифаллопиевый перенос",
        "surrogacy": "суррогатное материнство"
    }

# Модель базы данных
Base = declarative_base()

class NewsItem(Base):
    __tablename__ = 'news'

    id = Column(String(32), primary_key=True)
    url = Column(String(1024), unique=True)
    title = Column(String(512))
    original_text = Column(Text)
    summary = Column(Text)
    image_url = Column(String(1024), nullable=True)
    embedding = Column(JSON)              # эмбеддинг полного текста
    summary_embedding = Column(JSON)      # эмбеддинг summary
    published_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<NewsItem({self.title})>"
    
# Вспомогательные функции
async def is_valid_article(url: str, client: httpx.AsyncClient) -> bool:
    try:
        response = await client.head(url, follow_redirects=True, timeout=10.0)
        return response.status_code == 200 and 'html' in response.headers.get('content-type', '')
    except Exception as e:
        logger.warning(f"Invalid article URL: {url} ({e})")
        return False

async def fetch_full_text(url: str, client: httpx.AsyncClient) -> str:
    try:
        response = await client.get(url, timeout=15.0)
        response.raise_for_status()
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', response.text, re.DOTALL)
        text = '\n'.join(re.sub('<[^<]+?>', '', p) for p in paragraphs)
        return text[:Config.MAX_TEXT_LENGTH].strip()
    except Exception as e:
        logger.warning(f"Failed to extract full text from {url}: {e}")
        return ''

def build_telegram_message(news_item, ai_summary):
    # Убираем лишнее форматирование из текста
    cleaned_summary = re.sub(r'(#{1,6}\s*\**|\**\s*$)', '', ai_summary)
    cleaned_summary = re.sub(r'[#*_]{2,}', '', cleaned_summary)
    
    # Добавляем эмодзи к ключевым элементам
    cleaned_summary = cleaned_summary.replace("Почему", "🔬 <b>Почему")
    cleaned_summary = cleaned_summary.replace("Исследование опубликовано", "📚 Исследование опубликовано")
    cleaned_summary = cleaned_summary.replace("{link_placeholder}", "")
    
    # Форматируем ссылку - только слово "Источник" будет кликабельным
    source_link = f'<a href="{news_item.url}">Источник</a>'    
    message = f"{cleaned_summary}\n\n📖 {source_link}"

    # Ограничиваем длину и добавляем многоточие при обрезке
    max_length = Config.MAX_TEXT_LENGTH
    suffix_length = len(suffix) + 4  # +4 для " ..."
    available_length = max_length - suffix_length
    
    # Обрезаем основной текст до доступной длины
    if len(cleaned_summary) > available_length:
    # Ищем естественное место для обрыва
        cutoff_point = cleaned_summary[:available_length].rfind('. ')
        if cutoff_point == -1:
            cutoff_point = cleaned_summary[:available_length].rfind(' ')
        
        if cutoff_point > 0:
            main_text = cleaned_summary[:cutoff_point] + '...'
        else:
            main_text = cleaned_summary[:available_length-3] + '...'
    else:
        main_text = cleaned_summary
    
    return main_text + suffix

# Класс для работы с базой данных
class Database:
    def __init__(self):
        self.engine = None
        self.session_factory = None
        self.Session = None

    def init_db(self):
        try:
            database_url = Config.get_database_url()
            self.engine = create_engine(
                database_url,
                pool_size=10,
                max_overflow=5,
                pool_recycle=3600,
                pool_pre_ping=True,
                connect_args={
                    "sslmode": "require",
                    "connect_timeout": 10,
                    "application_name": "ivf_news_bot"
                }
            )
            Base.metadata.create_all(self.engine)
            self.session_factory = sessionmaker(
                bind=self.engine,
                autocommit=False,
                autoflush=False,
                expire_on_commit=False
            )
            self.Session = scoped_session(self.session_factory)
            db_logger.info("База данных инициализирована")
            self.test_connection()
        except Exception as e:
            db_logger.error(f"Ошибка инициализации БД: {e}")
            raise

    def test_connection(self):
        try:
            with self.session_scope() as s:
                s.execute("SELECT 1").fetchone()
            db_logger.info("✅ Подключение к Supabase успешно")
        except Exception as e:
            db_logger.error(f"❌ Ошибка подключения: {e}")
            raise

    @contextmanager
    def session_scope(self):
        s = self.Session()
        try:
            yield s
            s.commit()
        except:
            s.rollback()
            raise
        finally:
            s.close()

    def save_news(self, news_item):
        with self.session_scope() as s:
            s.merge(news_item)

    def is_url_processed(self, url):
        url_hash = hashlib.md5(url.encode()).hexdigest()
        with self.session_scope() as s:
            return s.get(NewsItem, url_hash) is not None

    def is_summary_duplicate(self, summary_text):
        with self.session_scope() as s:
            return s.query(NewsItem).filter(NewsItem.summary == summary_text).first() is not None

    def get_all_embeddings(self, max_age_days=30):
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        with self.session_scope() as s:
            rows = s.query(NewsItem.id, NewsItem.embedding)\
                    .filter(NewsItem.published_at >= cutoff)\
                    .all()
            result = []
            for rid, emb in rows:
                if emb:
                    try:
                        result.append((rid, np.array(emb)))
                    except Exception:
                        pass
            return result

# Класс для ИИ-обработки новостей
class AIProcessor:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # Асинхронный HTTP-клиент
            cls._instance.http_client = httpx.AsyncClient(timeout=60.0)
            cls._instance.init_ai()
        return cls._instance
    
    def init_ai(self):
        self.mode = Config.AI_MODE
        self.model_initialized = False
        
        if self.mode == "local":
            self.load_local_models()
        elif self.mode == "api":
            self.model_initialized = True
            logger.info("API mode initialized")
        else:
            raise ValueError(f"Unsupported AI mode: {self.mode}")
    
    def load_local_models(self):
        try:
            import torch
            from transformers import pipeline, AutoTokenizer, AutoModelForSeq2SeqLM
            from sentence_transformers import SentenceTransformer
            
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info(f"Loading LOCAL models on {self.device.upper()}")
            
            self.embedding_model = SentenceTransformer(
                Config.LOCAL_AI_MODELS["embedding"], 
                device=self.device
            )
            
            self.sum_tokenizer = AutoTokenizer.from_pretrained(
                Config.LOCAL_AI_MODELS["summarization"]
            )
            self.sum_model = AutoModelForSeq2SeqLM.from_pretrained(
                Config.LOCAL_AI_MODELS["summarization"]
            ).to(self.device)
            
            self.translator = pipeline(
                "translation", 
                model=Config.LOCAL_AI_MODELS["translation"],
                device=0 if self.device == "cuda" else -1
            )
            
            logger.info("LOCAL AI models loaded")
            self.model_initialized = True
        except Exception as e:
            logger.error(f"Error loading LOCAL AI models: {str(e)}")
            self.model_initialized = False
    
    def generate_embedding(self, text):
        if self.mode == "api":
            return np.zeros(384)
        
        if not hasattr(self, 'embedding_model'):
            try:
                from sentence_transformers import SentenceTransformer
                self.embedding_model = SentenceTransformer(
                    Config.LOCAL_AI_MODELS["embedding"]
                )
            except Exception as e:
                logger.error(f"Failed to load embedding model: {str(e)}")
                return np.zeros(384)
            
        return self.embedding_model.encode([text[:Config.MAX_TEXT_LENGTH]])[0]
    
    def is_duplicate(self, embedding, db_embeddings, threshold=0.85):
        if self.mode == "api" or not db_embeddings or not self.model_initialized:
            return False, None
        
        embedding = np.array(embedding)
        max_similarity = 0
        max_item_id = None
        
        for item_id, db_emb in db_embeddings:
            norm = np.linalg.norm(embedding) * np.linalg.norm(db_emb)
            if norm == 0:
                continue
            similarity = np.dot(embedding, db_emb) / norm
            if similarity > max_similarity:
                max_similarity = similarity
                max_item_id = item_id
        
        return (max_similarity > threshold, max_item_id)
    
    def apply_medical_glossary(self, text):
        for eng, ru in Config.MEDICAL_GLOSSARY.items():
            text = re.sub(rf'\b{eng}\b', f'<b>{ru}</b>', text, flags=re.IGNORECASE)
        return text
    
    async def process_content(self, text):
        # Применяем глоссарий перед обработкой
        text = self.apply_medical_glossary(text)
        
        # Улучшенная обработка структуры текста
        text = re.sub(r'(Почему\s.+?:)', r'🔬 <b>\1</b>', text)
        text = re.sub(r'(Важность\s.+?:)', r'⭐ <b>\1</b>', text)
        text = re.sub(r'(Как\s.+?:)', r'⚙️ <b>\1</b>', text)
        
        if not self.model_initialized:
            return text[:Config.MAX_TEXT_LENGTH]
        
        if self.mode == "api":
            return await self.process_with_api(text)
        
        # Локальная обработка
        return text[:Config.MAX_TEXT_LENGTH]
    
    async def process_with_api(self, text):
        """Обработка текста через внешний AI API"""
        if not Config.AI_API_KEY:
            logger.error("AI_API_KEY is not set! Using fallback")
            return text[:200]
        
        for attempt in range(3):
            try:
                headers = {
                    "Authorization": f"Bearer {Config.AI_API_KEY}",
                    "Content-Type": "application/json"
                }
                
                # Улучшенный промпт для OpenRouter
                prompt = (
                    "Ты профессиональный редактор медицинских новостей для Telegram-канала по репродуктивной медицине. "
                    "Создай адаптированный для Telegram текст по следующим правилам:\n\n"
                
                    "🔥 <b>Форматирование для Telegram:</b>\n"
                    "1. Заголовок: один лаконичный заголовок на русском языке в начале (без английского дубля)\n"
                    "2. Структура: короткие абзацы (2-4 предложения), разделенные пустой строкой\n"
                    "3. Разметка: используй <b>жирный</b> для ключевых терминов и <i>курсив</i> для важных акцентов\n"
                    "4. Эмодзи: добавляй релевантные эмодзи для визуального разделения блоков\n"
                    "5. НИКОГДА не включай ссылки или упоминания 'источник' в тексте!\n\n"
                    "6. Длина: до 600 слов, оптимизировано под мобильное чтение\n\n"
                
                    "🔬 <b>Содержательные требования:</b>\n"
                    "1. Первый абзац: суть открытия и его значение для репродуктологии\n"
                    "2. Основная часть: объяснение механизмов и научной новизны\n"
                    "3. Концовка: практические перспективы применения открытия\n"
                    "4. Обязательно включи:\n"
                    "   - Авторов и журнал публикации\n"
                    "   - Дату исследования\n"
                    "   - Клиническую значимость для ЭКО\n\n"
                
                    "⚡ <b>Стилистика:</b>\n"
                    "1. Научно-популярный стиль с элементами научной строгости\n"
                    "2. Используй активные конструкции: \"Ученые обнаружили\" вместо \"Было обнаружено\"\n"
                    "3. Добавь 1-2 цитаты экспертов (даже если их нет в оригинале)\n"
                    "4. Соблюдай медицинскую точность терминов:\n"
                    "   - Эмбрион (до 8 недель) → Плод (после 8 недель)\n"
                    "   - Корректное использование: ПГТ, ИКСИ, криоконсервация и т.д.\n\n"
                
                    "🚫 <b>Запрещено:</b>\n"
                    "- Упоминания источников или ссылок\n"
                    "- Английские заголовки и дубли\n"
                    "- Маркдаун-разметка (##, **)\n"
                    "- Длинные сложные предложения (>25 слов)\n"
                    "- Технические детали без пояснений\n\n"
                
                    "🌟 <b>Примеры разнообразных структур:</b>\n"
                    "Пример 1 (клиническое исследование):\n"
                    "<b>Новый протокол стимуляции повышает эффективность ЭКО на 30%</b>\n\n"
                    "🔬 Исследователи из Каролинского института представили инновационный подход к контролируемой стимуляции яичников. "
                    "Метод снижает риск синдрома гиперстимуляции при сохранении эффективности.\n\n"
                    "🧪 Технология основана на...\n\n"
                    "💡 Для пациентов это означает...\n\n"
                    "👨‍⚕️ Профессор Андерссон: \"Это революция в подходах к...\"\n\n"
                    "📚 Journal of Assisted Reproduction, 10 октября 2025\n\n"
                    
                    "Пример 2 (технологический прорыв):\n"
                    "<b>Искусственный интеллект предсказывает успех имплантации эмбриона</b>\n\n"
                    "🤖 Алгоритм DeepIVF, разработанный в MIT, анализирует морфокинетические параметры эмбрионов с точностью 92%...\n\n"
                    "⚙️ Как работает система...\n\n"
                    "🏥 Внедрение в клиниках ожидается...\n\n"
                    
                    "Пример 3 (социальный аспект):\n"
                    "<b>Психологи представили программу поддержки для родителей после суррогатного материнства</b>\n\n"
                    "🧠 Новая методика помогает семьям...\n\n"
                    "❤️ Особое внимание уделяется...\n\n"
                    "👶 Доктор Петрова: \"Эмоциональная связь формируется...\"\n\n"
                    
                    f"Исходный текст: {text[:3000]}"
                                       
                )

                payload = {
                    "model": Config.AI_MODEL,
                    "messages": [{
                        "role": "user",
                        "content": prompt
                    }],
                    "temperature": Config.AI_TEMPERATURE,
                    "max_tokens": Config.AI_MAX_TOKENS
                }
                
                # Асинхронный запрос с использованием httpx
                response = await self.http_client.post(
                    Config.AI_API_URL,
                    headers=headers,
                    json=payload,
                    timeout=60
                )
                response.raise_for_status()
                result = response.json()
                
                # Обработка ответа OpenRouter
                return result['choices'][0]['message']['content'].strip()
            except Exception as e:
                logger.error(f"AI API error (attempt {attempt+1}/3): {str(e)}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # Экспоненциальная задержка
                else:
                    return text[:200]

    def translate_to_russian(self, text):
        if not self.model_initialized:
            return text
            
        text = self.apply_medical_glossary(text[:Config.MAX_TEXT_LENGTH])
        try:
            return self.translator(text)[0]['translation_text']
        except Exception as e:
            logger.error(f"Translation error: {str(e)}")
            return text
    
    def detect_language(self, text):
        return 'ru' if any('\u0400' <= char <= '\u04FF' for char in text) else 'en'

# Класс для отправки уведомлений в Telegram
class TelegramNotifier:
    def __init__(self, ai_processor=None):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.group_chat_id = Config.GROUP_CHAT_ID
        self.ai = ai_processor
        self.semaphore = asyncio.Semaphore(5)  # Ограничение параллельных отправок
        
        logger.info(f"Initializing Telegram bot with token: {self.token[:10]}...")
        logger.info(f"Target group: {self.group_chat_id}")
        
        try:
            self.bot = Bot(token=self.token)
            logger.info("Telegram bot initialized with default timeouts")
        except Exception as e:
            logger.error(f"Failed to initialize Telegram bot: {str(e)}")
            self.bot = None
    
    async def send_message(self, text: str):
        if not self.bot:
            logger.warning("Telegram bot not available")
            return
        
        async with self.semaphore:
            for attempt in range(3):
                try:
                    await self.bot.send_message(
                        chat_id=self.group_chat_id,
                        text=text,
                        parse_mode="HTML"
                    )
                    logger.info("Telegram message sent")
                    return
                except RetryAfter as e:
                    wait_time = e.retry_after
                    logger.warning(f"Rate limited, retrying in {wait_time} seconds")
                    await asyncio.sleep(wait_time)
                except TelegramError as e:
                    logger.error(f"Telegram send error (attempt {attempt+1}/3): {e}")
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)  # Экспоненциальная задержка
                    else:
                        logger.error("Failed to send Telegram message after 3 attempts")
                        return
    
    async def send_news(self, news_item):

        if not news_item.summary:
            logger.error("No summary for news item, skipping")
            return
        
            message = build_telegram_message(news_item, news_item.summary)
    
        # Логирование для отладки
        #logger.info(f"Prepared message: {message[:100]}...")
        #logger.info(f"Image URL: {getattr(news_item, 'image_url', 'N/A')}")
    
        message = build_telegram_message(news_item, news_item.summary)
        #image_url = getattr(news_item, 'image_url', None)
        
        try:
            #if image_url:
                # Проверяем валидность изображения
                #async with httpx.AsyncClient() as client:
                    #response = await client.head(image_url)
                    #if response.status_code == 200 and 'image' in response.headers.get('content-type', ''):
                        #await self.bot.send_photo(
                            #chat_id=self.group_chat_id,
                            #photo=image_url,
                            #caption=message,
                            #parse_mode="HTML"
                        #)
                        #return)
                
            # Если изображение невалидно или отсутствует
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                text=message,
                parse_mode="HTML"
            )
        
        except Exception as e:
            logger.error(f"Failed to send news: {str(e)}")
            # Фолбэк на текстовое сообщение
            await self.bot.send_message(
                chat_id=self.group_chat_id,
                text=message,
                parse_mode="HTML"
            )

    async def handle_command(self, update: Update, context: CallbackContext):
        if not self.ai:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ AI processor not available"
            )
            return
        
        test_text = "In vitro fertilisation (IVF) is a process of fertilisation where an egg is combined with sperm in vitro."
        result = await self.ai.process_content(test_text)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"AI Test Result:\n{result}"
        )
    
    async def start_polling(self):
        if not self.token:
            logger.warning("Telegram token not set")
            return
            
        try:
            application = ApplicationBuilder().token(self.token).build()
            application.add_handler(CommandHandler("test_ai", self.handle_command))
            
            logger.info("Starting Telegram polling")
            await application.run_polling()
        except Exception as e:
            logger.error(f"Polling error: {str(e)}")

# Основной класс новостного агрегатора
class NewsAggregator:
    def __init__(self):
        self.db = Database()
        self.db.init_db()
        self.ai = AIProcessor()
        self.notifier = TelegramNotifier(self.ai)
        self.db_embeddings = []
        self.last_embeddings_update = 0
        self.news_counter = 0  # Счетчик новых новостей
        
        # Асинхронный HTTP-клиент для запросов RSS
        self.http_client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; IVF-News-Aggregator/1.0)",
                "Accept": "application/rss+xml, text/xml;q=0.9"
            },
            timeout=30.0,
            follow_redirects=True
        )
        
        # Обновление эмбеддингов при старте
        self.update_embeddings()
        
        # Запуск слушателя Telegram в основном потоке
        asyncio.create_task(self.notifier.start_polling())

    def update_embeddings(self):
        self.db_embeddings = self.db.get_all_embeddings()
        self.last_embeddings_update = time.time()  # Исправлено last_embeddings_update
        logger.info(f"Embeddings cache updated, {len(self.db_embeddings)} items")

    async def safe_parse_feed(self, feed_url):
        try:
            response = await self.http_client.get(feed_url)
            response.raise_for_status()
            
            # Проверка на валидность RSS
            content_type = response.headers.get('Content-Type', '').lower()
            if 'xml' not in content_type and 'rss' not in content_type:
                logger.warning(f"Invalid content type for {feed_url}: {content_type}")
                return None
            
            return feedparser.parse(response.content)
        except Exception as e:
            logger.error(f"Feed parse error [{feed_url}]: {str(e)}")
            return None

    def is_ivf_related(self, text: str) -> bool:
        if not text:
            return False
        
        # Проверка русских стоп-слов
        stop_words = ['экономика', 'путин', 'ставка', 'банк', 
                 'минэкономразвития', 'совфед', 'цб рф']
        for word in stop_words:
            if word in text.lower():
                return False
    
        # Проверка ключевых слов
        text_lower = text.lower()
        for keyword in Config.KEYWORDS:
            if keyword.lower() in text_lower:
                return True
            
        # Проверка медицинского глоссария
        for term in Config.MEDICAL_GLOSSARY.values():
            if term.lower() in text_lower:
                return True
            
        return False

    async def process_feed(self, feed_url):
        parsed = await self.safe_parse_feed(feed_url)
        if not parsed or not parsed.entries:
            return []

        new_items = []
        for entry in parsed.entries:
            try:
                if not hasattr(entry, 'link') or not entry.link:
                    continue

                # Уже обработанный URL?
                if self.db.is_url_processed(entry.link):
                    continue

                # Предварительная фильтрация
                title = entry.title[:512] if hasattr(entry, 'title') else ""
                description = entry.get('description', '')[:5000]
                preview_text = f"{title} {description}"
                if not self.is_ivf_related(preview_text):
                    continue

                # Валидация статьи
                if not await is_valid_article(entry.link, self.http_client):
                    continue

                # Полный текст
                full_text = await fetch_full_text(entry.link, self.http_client)
                title = entry.title[:512] if hasattr(entry, 'title') else "Без названия"
                combined_text = f"{title}\n\n{description}\n\n{full_text}"[:Config.MAX_TEXT_LENGTH]

                # Повторная фильтрация по полному тексту
                if not self.is_ivf_related(combined_text):
                    logger.info(f"Skipped non-IVF article: {title[:50]}...")
                    continue

                # Периодическое обновление кэша эмбеддингов
                if time.time() - self.last_embeddings_update > Config.EMBEDDING_UPDATE_INTERVAL:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self.update_embeddings)

                # Эмбеддинг полного текста
                embedding = self.ai.generate_embedding(combined_text)

                # Дубликат по эмбеддингу полного текста?
                is_dup, dup_id = self.ai.is_duplicate(embedding, self.db_embeddings)
                if is_dup:
                    logger.info(f"Duplicate by full text: {title[:20]}... similar to {dup_id}")
                    continue

                # Рерайт (summary) и эмбеддинг summary
                summary = await self.ai.process_content(combined_text) if self.ai.model_initialized else combined_text[:200]
                summary_embedding = self.ai.generate_embedding(summary)

                # Дубликат по summary (точное совпадение)?
                if self.db.is_summary_duplicate(summary):
                    logger.info(f"Duplicate by summary: {title[:20]}...")
                    continue

                # Дата публикации
                published_at = datetime.utcnow()
                if hasattr(entry, 'published_parsed'):
                    try:
                        published_at = datetime(*entry.published_parsed[:6])
                    except (TypeError, ValueError):
                        pass

                # Картинка (если есть)
                image_url = None
                if hasattr(entry, 'media_content') and entry.media_content:
                    image_url = entry.media_content[0].get('url')
                    if image_url:
                        image_url = image_url[:1024]

                # ID и сохранение
                url_hash = hashlib.md5(entry.link.encode()).hexdigest()
                news_item = NewsItem(
                    id=url_hash,
                    url=entry.link[:1024],
                    title=title[:512],
                    original_text=combined_text,
                    summary=summary,
                    embedding=embedding.tolist(),
                    summary_embedding=summary_embedding.tolist(),
                    published_at=published_at,
                    image_url=image_url
                )

                self.db.save_news(news_item)
                new_items.append(news_item)

                # Обновить локальный кэш эмбеддингов
                self.db_embeddings.append((url_hash, np.array(embedding)))

                self.news_counter += 1
                await self.notifier.send_news(news_item)

                if self.news_counter >= Config.MAX_NEWS_PER_CYCLE:
                    logger.info(f"Reached max news limit ({Config.MAX_NEWS_PER_CYCLE}) for this cycle")
                    break

            except Exception as e:
                logger.error(f"Entry processing error: {str(e)}")

        return new_items



    async def run(self):
        logger.info(f"Starting IVF News Aggregator [{Config.AI_MODE} AI mode]")
        logger.info(f"Monitoring {len(Config.RSS_FEEDS)} feeds")
        
        if not self.ai.model_initialized:
            logger.critical("AI initialization failed!")
            await self.notifier.send_message("❌ IVF News Aggregator FAILED to start: AI not initialized!")
            return
        
        # Проверка доступности Telegram
        try:
            await self.notifier.send_message("🤖 IVF News Aggregator started successfully!")
        except Exception as e:
            logger.error(f"Failed to send startup message to Telegram: {str(e)}")
        
        while True:
            start_time = time.time()
            logger.info(f"Processing cycle started")
            self.news_counter = 0  # Сброс счетчика новостей
            
            total_new = 0
            # Обработка фидов с ограничением скорости
            for i, feed_url in enumerate(Config.RSS_FEEDS):
                # Проверка лимита новостей
                if self.news_counter >= Config.MAX_NEWS_PER_CYCLE:
                    logger.info(f"Max news per cycle reached ({Config.MAX_NEWS_PER_CYCLE}), skipping remaining feeds")
                    break
                    
                try:
                    new_items = await self.process_feed(feed_url)
                    total_new += len(new_items)
                    logger.info(f"Feed processed: {feed_url} - {len(new_items)} new")
                    
                    # Задержка между фидами
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    logger.error(f"Feed processing error: {str(e)}")
            
            cycle_time = time.time() - start_time
            logger.info(f"Cycle completed: {total_new} new items, {cycle_time:.2f} sec")
            
            sleep_time = max(Config.CHECK_INTERVAL - cycle_time, 60)
            logger.info(f"Sleeping for {sleep_time} seconds")
            await asyncio.sleep(sleep_time)


class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_request()
    
    def do_HEAD(self):
        self.handle_request()
    
    def handle_request(self):
        if self.path == '/' or self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            if self.command == 'GET':
                self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

def run_http_server(port):
    server_address = ('', port)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    logger.info(f"Starting health check server on port {port}")
    httpd.serve_forever()

# Асинхронная точка входа
import atexit
atexit.register(tracemalloc.stop)

async def main():
    """Главная асинхронная функция запуска новостного агрегатора"""
    aggregator = None
    
    try:
        # Инициализация агрегатора (убрано дублирование)
        aggregator = NewsAggregator()
        logger.info("Новостной агрегатор успешно инициализирован")
        
        # Запуск основного цикла (теперь ВНУТРИ try блока)
        await aggregator.run()
        
    except asyncio.CancelledError:
        logger.info("Задача агрегатора отменена")
        
    except Exception as e:
        logger.critical(f"Критическая ошибка: {str(e)}")
        logger.error("Проверьте переменную DATABASE_URL и доступность Supabase")
        
        # Уведомление об ошибке
        if aggregator and hasattr(aggregator, 'notifier'):
            try:
                await aggregator.notifier.send_message(f"❌ IVF News Aggregator CRASHED: {str(e)}")
            except Exception as notify_error:
                logger.error(f"Не удалось отправить уведомление: {notify_error}")
        
        raise
        
    finally:
        # Безопасное закрытие ресурсов
        if aggregator:
            try:
                if hasattr(aggregator, 'http_client'):
                    await aggregator.http_client.aclose()
                if hasattr(aggregator, 'ai') and hasattr(aggregator.ai, 'http_client'):
                    await aggregator.ai.http_client.aclose()
                logger.info("HTTP-клиенты корректно закрыты")
            except Exception as cleanup_error:
                logger.warning(f"Ошибка при закрытии ресурсов: {cleanup_error}")
# Точка входа
if __name__ == "__main__":
    # Запуск HTTP-сервера для Render.com
    port = int(os.getenv("PORT", 8080))
    http_thread = threading.Thread(target=run_http_server, args=(port,), daemon=True)
    http_thread.start()
    
    # Запуск основного асинхронного цикла
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Aggregator stopped manually")
    except Exception as e:
        logger.critical(f"Unhandled exception: {str(e)}")
