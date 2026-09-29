""" User accounts for logging in to the application """
import datetime
from enum import IntEnum
import argon2
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
