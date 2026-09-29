DEBUG = False
TESTING = False

MONGODB_URI = "mongodb://127.0.0.1:27017/bgexplorer3_testing"
# SECRET_KEY must be set outside of debug and testing, e.g. with the
# FLASK_SECRET_KEY environment variable. Anyone who knows it can log in as
# any user.
SECRET_KEY = None
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
# set True when served over https
SESSION_COOKIE_SECURE = False
