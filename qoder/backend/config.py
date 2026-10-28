import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    POWERCONTEXT_API_URL = os.getenv('POWERCONTEXT_API_URL', 'http://localhost:8848')
    POWERCONTEXT_SCOPE_ID = os.getenv('POWERCONTEXT_SCOPE_ID', 'default_learning_scope')
    
    SEEKDB_HOST = os.getenv('SEEKDB_HOST', 'localhost')
    SEEKDB_PORT = int(os.getenv('SEEKDB_PORT', '2881'))
    SEEKDB_USER = os.getenv('SEEKDB_USER', 'root')
    SEEKDB_PASSWORD = os.getenv('SEEKDB_PASSWORD', '')
    SEEKDB_DATABASE = os.getenv('SEEKDB_DATABASE', 'study_mind')
    
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
    OPENAI_BASE_URL = os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-3.5-turbo')
    OPENAI_EMBEDDING_MODEL = os.getenv('OPENAI_EMBEDDING_MODEL', 'text-embedding-3-small')
    
    FLASK_HOST = os.getenv('FLASK_HOST', '0.0.0.0')
    FLASK_PORT = int(os.getenv('FLASK_PORT', '5000'))
    FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'True') == 'True'
