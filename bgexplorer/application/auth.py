from functools import wraps
from ..models.settings import User, Permission
from flask_login import LoginManager, current_user, login_user, logout_user
from flask import request, current_app
from bson import ObjectId
from enum import Enum
import logging
log = logging.Logger(__name__)

login_manager = LoginManager()

class Permission(Flag):
    """ Permission levels are additive (higher levels inherit all below)
    except site-admin, which is handled by a dedicated additional login
    """
    view = 1
    edit = 2
    create_tags = 4
    delete_tags = 8
    site_admin = 256

    user_edit = 3
    user_create_tags = 7
    user_delete_tags = 15
    user_site_admin = 257


class User(UserMixin, Document):
    name = StringField(unique=True)
    pwhash = BinaryField(required=True)
    permissions = EnumField(Permission, required=True,
                            default=Permission.view)

    @property
    def hasher(self):
        return argon2.PasswordHasher()

    def set_password(self, password):
        self.pwhash = self.hasher.hash(password)
        self.save()

    def test_password(self, password):
        try:
            return self.hasher.verify(self.pwhash, password)
        except argon2.exceptions.VerifyMismatchError:
            return False
        # exceptions for bad hash will still be raised

@login_manager.user_loader
def load_user(userid):
    try:
        return User.get(id=ObjectId(userid))
    except:
        pass

def login():
    error = None
    if flask.request.method == 'POST':
        username = flask.request.form['username']
        password = flask.request.form['password']
        try:
            user = User.objects.get(name=username)
            pwvalid = user.test_password(password)
        except:
            pass
        if pwvalid:
            flask.flash(f"Successfully logged in as {user.name}")
            login_user(user)
            next_ = flask.request.args.get('next', flask.url_for('index'))
            return flask.redirect(next_)
        else:
            error = "Error logging in with the provided username and password"
    return render_template('login.html', error=error)


def logout():
    logout_user()
    flask.flash("User successfully logged out", 'success')
    return flask.redirect(flask.url_for('index')

# TODO: register login_manager.unauthorized as 401 handler
# TODO: make this act as an extension
def check_permission(perm):
    perm = Permissions(perm)
    if current_app.config.get('LOGIN_DISABLED'):
        return
    try:
        valid = perm in current_user.permissions
    except AttributeError:
        valid = (perm == Permission.view and
                 get_application_settings().allow_anon_view)
    if not valid:
        return login_handler.unauthorized()
    return

def permission_required(perm: Permission):
    """ decorator for endpoints that require specific permissions """
    def decorator(func):
        @wraps(func)
        def decorated_func(*args, **kwargs):
            if response := check_permission(perm):
                return response
            # return current_app.ensure_sync(func)(*args, **kwargs)
            return func(*args, **kwargs)
        return decorated_func
