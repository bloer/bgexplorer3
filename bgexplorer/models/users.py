""" User accounts for logging in to the application """
import datetime
import hashlib
import hmac
from enum import IntEnum
from typing import Optional
import argon2
from bson import ObjectId
from mongoengine import (Document, StringField, EnumField, BooleanField,
                         DateTimeField)

MIN_PASSWORD_LENGTH = 8

_hasher = argon2.PasswordHasher()


class Role(IntEnum):
    """ Roles are ordered: each can do everything the ones below it can """
    viewer = 1  # read everything, when anonymous viewing is off
    editor = 2  # edit documents and branches
    admin = 3  # create tags and other irreversible changes
    site_admin = 4  # site settings, maintenance and user accounts


class User(Document):
    name = StringField(required=True, unique=True)
    pwhash = StringField()
    role = EnumField(Role, required=True, default=Role.viewer)
    active = BooleanField(default=True)
    created = DateTimeField(default=datetime.datetime.now)

    meta = {'ordering': ['name']}

    def __str__(self):
        return self.name

    def set_password(self, password: str) -> None:
        """ Set (but don't save) the password. Raise ValueError if it is
        too short
        """
        if len(password or '') < MIN_PASSWORD_LENGTH:
            raise ValueError("Passwords must have at least "
                             f"{MIN_PASSWORD_LENGTH} characters")
        self.pwhash = _hasher.hash(password)

    def check_password(self, password: str) -> bool:
        """ Whether `password` is correct. Updates the stored hash if the
        hashing parameters have changed
        """
        if not self.pwhash or not password:
            return False
        try:
            _hasher.verify(self.pwhash, password)
        except (argon2.exceptions.VerificationError,
                argon2.exceptions.InvalidHashError):
            # VerifyMismatchError is a VerificationError
            return False
        if _hasher.check_needs_rehash(self.pwhash):
            self.pwhash = _hasher.hash(password)
            if self.pk:
                self.save()
        return True

    def has_role(self, role: Role) -> bool:
        return bool(self.active) and self.role >= role

    # the flask-login user protocol, without depending on flask here
    is_authenticated = True
    is_anonymous = False

    @property
    def is_active(self) -> bool:
        return bool(self.active)

    @property
    def session_token(self) -> str:
        """ Changes with the password, ending sessions logged in with the
        old one
        """
        return hashlib.sha256((self.pwhash or '').encode()).hexdigest()[:16]

    def get_id(self) -> str:
        return f"{self.id}:{self.session_token}"

    @classmethod
    def from_session_id(cls, session_id: str) -> Optional['User']:
        """ The active user for a `get_id` value, or None """
        userid, _, token = str(session_id).partition(':')
        if not ObjectId.is_valid(userid):
            return None
        user = cls.objects(id=ObjectId(userid), active=True).first()
        if user and hmac.compare_digest(user.session_token, token):
            return user
        return None


def check_login(name: str, password: str) -> Optional[User]:
    """ The active user with `name` and `password`, or None. Takes about as
    long for unknown names as for wrong passwords
    """
    user = User.objects(name=(name or '').strip(), active=True).first()
    if user is None:
        User(pwhash=_dummy_hash()).check_password(password)
        return None
    return user if user.check_password(password) else None


_dummy = []


def _dummy_hash() -> str:
    if not _dummy:
        _dummy.append(_hasher.hash('not a real password'))
    return _dummy[0]
