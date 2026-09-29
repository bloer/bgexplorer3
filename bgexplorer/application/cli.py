""" Command line tools for the server, run with `flask --app bgexplorer` """
import click
from flask.cli import with_appcontext
from mongoengine.errors import NotUniqueError
from ..models.users import User, Role

ROLE_NAMES = [role.name for role in Role]


def _set_password(user: User, password: str) -> None:
    try:
        user.set_password(password)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint='password')


@click.command('create-user')
@click.argument('name')
@click.option('--role', type=click.Choice(ROLE_NAMES), default='viewer',
              show_default=True)
@click.password_option()
@with_appcontext
def create_user(name, role, password):
    """ Create a user account, e.g. the first site_admin """
    user = User(name=name.strip(), role=Role[role])
    _set_password(user, password)
    try:
        user.save()
    except NotUniqueError:
        raise click.ClickException(f"There is already a user named '{name}'")
    click.echo(f"Created {role} '{user.name}'")


@click.command('set-password')
@click.argument('name')
@click.password_option()
@with_appcontext
def set_password(name, password):
    """ Set a user's password, logging them out """
    user = User.objects(name=name).first()
    if user is None:
        raise click.ClickException(f"No user named '{name}'")
    _set_password(user, password)
    user.save()
    click.echo(f"Set the password for '{name}'")


def init_app(app) -> None:
    app.cli.add_command(create_user)
    app.cli.add_command(set_password)
