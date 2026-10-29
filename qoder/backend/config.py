import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    POWERCONTEXT_API_URL = os.getenv('POWERCONTEXT_API_URL', 'http://localhost:8848')
    POWERCONTEXT_SCOPE_ID = os.getenv('POWERCONTEXT_SCOPE_ID', 'default_learning_scope')
    
    # Leave SEEKDB_HOST unset to prefer an embedded seekdb (Linux, needs
    # pylibseekdb); set it when you run a seekdb server and want to use it.
    SEEKDB_HOST = os.getenv('SEEKDB_HOST', '')
    SEEKDB_PATH = os.getenv('SEEKDB_PATH', '')
    SEEKDB_PORT = int(os.getenv('SEEKDB_PORT', '2881'))
    SEEKDB_USER = os.getenv('SEEKDB_USER', 'root')
    SEEKDB_PASSWORD = os.getenv('SEEKDB_PASSWORD', '')
    SEEKDB_DATABASE = os.getenv('SEEKDB_DATABASE', 'study_mind')
    
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-3.5-turbo')
    OPENAI_EMBEDDING_MODEL = os.getenv('OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')
    
    # Hosting defaults that work both locally and inside a container:
    # honour the generic PORT/HOST variables that most runners inject, and
    # never enable the reloader by default (it forks a second process and makes
    # automated health checks flaky).
    FLASK_HOST = os.getenv('HOST') or os.getenv('FLASK_HOST', '0.0.0.0')
    FLASK_PORT = int(os.getenv('PORT') or os.getenv('FLASK_PORT') or '5000')
    FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').strip().lower() in ('1', 'true', 'yes')
