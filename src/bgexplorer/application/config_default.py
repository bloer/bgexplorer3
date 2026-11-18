DEBUG = False
TESTING = False

MONGODB_URI = "mongodb://127.0.0.1:27017/bgexplorer3_testing"
# SECRET_KEY protects login sessions: anyone who knows it can log in as any
# user. If it isn't set, e.g. with the FLASK_SECRET_KEY environment variable,
# a key is generated once and stored in the database.
SECRET_KEY = None
# static files are cached for a year; their urls change when the file does
SEND_FILE_MAX_AGE_DEFAULT = 31536000
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
# set True when served over https
SESSION_COOKIE_SECURE = False
