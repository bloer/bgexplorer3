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

# Background jobs: slow edits run as jobs that the web UI and API can watch.
# JOBS_MODE is 'thread' or 'process' (a pool in each server process), or
# 'eager' to run jobs inside the request, which is the default for TESTING.
JOBS_MODE = None
# jobs run at once in each server process; None means the CPU count, up to 4
JOBS_WORKERS = None
# how long a request waits for its job before sending the user to the
# status page
JOBS_GRACE_SECONDS = 2.5
# how long an API request waits for its job before answering 202 Accepted
JOBS_API_WAIT_SECONDS = 100
# a running job whose server hasn't renewed its lease for this long is
# considered dead
JOBS_LEASE_SECONDS = 60
# how often idle workers look for new jobs
JOBS_POLL_SECONDS = 1
# the least time between writes of a job's progress
JOBS_PROGRESS_INTERVAL = 0.5
# how long ended jobs are kept
JOBS_KEEP_DAYS = 7
# how long to let running jobs finish when the server stops
JOBS_SHUTDOWN_SECONDS = 20
# where uploaded and exported version files wait for the jobs that use them,
# which must be a place that every server process and worker can reach. None
# means 'bgexplorer-jobs' in the temporary directory
JOBS_FILES_DIR = None
