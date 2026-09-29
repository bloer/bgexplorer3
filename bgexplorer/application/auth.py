""" Logging in, and checking the current user's role

Roles are checked here, in the application layer. Read-only versions are
still enforced separately by the models (`check_writable`).

With the LOGIN_DISABLED config option, everyone has every role.
"""
from functools import wraps
from typing import Optional
from urllib.parse import urlsplit
import flask
from flask_login import LoginManager, current_user, login_user, logout_user
from ..models.settings import get_application_settings
from ..models.users import User, Role, check_login
from ..models.verdoc import set_user_provider

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = "Please log in to see this page"
login_manager.login_message_category = 'warning'

# endpoints anyone can reach, even if anonymous viewing is off
PUBLIC_ENDPOINTS = {'auth.login', 'auth.logout', 'static', 'favicon',
                    'admin.logo'}


@login_manager.user_loader
def load_user(session_id: str) -> Optional[User]:
    return User.from_session_id(session_id)


def login_disabled() -> bool:
    return bool(flask.current_app.config.get('LOGIN_DISABLED'))


def current_role() -> Optional[Role]:
    """ The role of the current user, if any. Anonymous users are viewers
    if the application settings allow it
    """
    if login_disabled():
        return Role.site_admin
    if current_user.is_authenticated:
        return current_user.role
    if get_application_settings().allow_anon_view:
        return Role.viewer
    return None


def has_role(role) -> bool:
    """ Whether the current user has `role` (a Role or its name) """
    if isinstance(role, str):
        role = Role[role]
    have = current_role()
    return have is not None and have >= role


def require(role: Role):
    """ Check that the current user has `role`. Returns a response to
    log in if anonymous, raises 403 for a logged in user without the role,
    or returns None if allowed
    """
    if has_role(role):
        return None
    if not current_user.is_authenticated:
        if flask.request.path.startswith('/api/'):
            flask.abort(401, "Log in to use this endpoint")
        return login_manager.unauthorized()
    flask.abort(403, f"This requires the {role.name} role")


def role_required(role: Role):
    """ View decorator: the current user must have `role` """
    def decorator(func):
        @wraps(func)
        def decorated(*args, **kwargs):
            if (response := require(role)) is not None:
                return response
            return func(*args, **kwargs)
        return decorated
    return decorator


SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


def require_for_changes(role: Role, form_endpoints=()):
    """ A before_request function for a blueprint: requests that may change
    something (anything but GET), and GETs of the endpoints named in
    `form_endpoints` (pages that only show a form), need `role`. New POST
    routes are then protected by default
    """
    def check():
        endpoint = (flask.request.endpoint or '').rsplit('.', 1)[-1]
        if (flask.request.method not in SAFE_METHODS
                or endpoint in form_endpoints):
            return require(role)
        return None
    return check


def safe_next(target: Optional[str]) -> str:
    """ `target` if it is a path on this site, else the index """
    if target:
        parts = urlsplit(target)
        if (not parts.scheme and not parts.netloc and target.startswith('/')
                and not target.startswith('//') and '\\' not in target):
            return target
    return flask.url_for('index')


def create_auth_blueprint() -> flask.Blueprint:
    bp = flask.Blueprint('auth', __name__)

    @bp.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if flask.request.method == 'POST':
            form = flask.request.form
            user = check_login(form.get('username'), form.get('password'))
            if user is not None:
                # a new session, so an old session id can't be reused
                flask.session.clear()
                login_user(user)
                flask.flash(f"Logged in as {user.name}", 'success')
                return flask.redirect(safe_next(flask.request.args.get('next')))
            error = "Unknown user name or wrong password"
        return flask.render_template('login.html', error=error), \
            401 if error else 200

    @bp.post('/logout')
    def logout():
        logout_user()
        flask.flash("Logged out", 'success')
        return flask.redirect(flask.url_for('index'))

    @bp.route('/profile', methods=['GET', 'POST'])
    def profile():
        if not current_user.is_authenticated:
            if login_disabled():
                return flask.render_template('profile.html', errors={})
            return login_manager.unauthorized()
        errors = {}
        if flask.request.method == 'POST':
            form = flask.request.form
            new = form.get('new_password', '')
            if not current_user.check_password(form.get('current_password')):
                errors['current_password'] = "Wrong password"
            elif new != form.get('confirm_password'):
                errors['confirm_password'] = "The passwords don't match"
            else:
                try:
                    current_user.set_password(new)
                except ValueError as e:
                    errors['new_password'] = str(e)
            if not errors:
                current_user.save()
                # the session id changes with the password
                login_user(current_user._get_current_object())
                flask.flash("Password changed", 'success')
                return flask.redirect(flask.url_for('.profile'))
        return flask.render_template('profile.html', errors=errors), \
            400 if errors else 200

    return bp


def _entered_by() -> Optional[str]:
    """ The name of the logged in user making a change, for `enteredby` """
    if flask.has_request_context() and current_user.is_authenticated:
        return current_user.name
    return None


def init_app(app: flask.Flask) -> None:
    """ Set up logins for `app`. Call before registering other
    before_request functions, so that login is checked first
    """
    login_manager.init_app(app)
    app.register_blueprint(create_auth_blueprint())

    @app.before_request
    def check_view_permission():
        if flask.request.endpoint in PUBLIC_ENDPOINTS:
            return None
        return require(Role.viewer)

    set_user_provider(_entered_by)
    app.add_template_global(has_role, 'has_role')
    app.add_template_global(login_disabled, 'login_disabled')
