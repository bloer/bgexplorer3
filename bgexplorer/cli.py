""" Command line tools for user accounts. These only need the database, not
the web app or its configuration:

    bgexplorer-users [--uri URI] create NAME --role site_admin
    python -m bgexplorer.cli [--uri URI] set-password NAME
"""
import click
import mongoengine
from mongoengine.errors import NotUniqueError
from .application.config_default import MONGODB_URI
from .models.users import User, Role

ROLE_NAMES = [role.name for role in Role]


@click.group()
@click.option('--uri', envvar='FLASK_MONGODB_URI', default=MONGODB_URI,
              show_default=True,
              help="MongoDB server and database, as for the server "
                   "[env var: FLASK_MONGODB_URI]")
def cli(uri):
    """ Manage bgexplorer user accounts """
    mongoengine.connect(host=uri)


def _set_password(user: User, password: str) -> None:
    try:
        user.set_password(password)
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint='password')


@cli.command()
@click.argument('name')
@click.option('--role', type=click.Choice(ROLE_NAMES), default='viewer',
              show_default=True)
@click.password_option()
def create(name, role, password):
    """ Create a user account, e.g. the first site_admin """
    user = User(name=name.strip(), role=Role[role])
    _set_password(user, password)
    try:
        user.save()
    except NotUniqueError:
        raise click.ClickException(f"There is already a user named '{name}'")
    click.echo(f"Created {role} '{user.name}'")


@cli.command('set-password')
@click.argument('name')
@click.password_option()
def set_password(name, password):
    """ Set a user's password, logging them out """
    user = User.objects(name=name).first()
    if user is None:
        raise click.ClickException(f"No user named '{name}'")
    _set_password(user, password)
    user.save()
    click.echo(f"Set the password for '{name}'")


def main():
    cli()


if __name__ == '__main__':
    main()
