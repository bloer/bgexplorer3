""" Command line tools for user accounts and versions. These only need the
database, not the web app or its configuration:

    bgexplorer-users [--uri URI] create NAME --role site_admin
    python -m bgexplorer.cli [--uri URI] set-password NAME
    bgexplorer-versions [--uri URI] export NAME [-o FILE]
    bgexplorer-versions [--uri URI] import FILE --name NEW
    bgexplorer-jobs [--uri URI] list
"""
import click
import mongoengine
from mongoengine.errors import NotUniqueError
from .application.config_default import MONGODB_URI
from .models.users import User, Role
from .jobs import maintenance as job_maintenance, models as job_models
from .versioning import control as vc
from .versioning.archive import (export_version, import_version,
                                 VersionFileError)

ROLE_NAMES = [role.name for role in Role]


uri_option = click.option(
    '--uri', envvar='FLASK_MONGODB_URI', default=MONGODB_URI,
    show_default=True,
    help="MongoDB server and database, as for the server "
         "[env var: FLASK_MONGODB_URI]")


@click.group()
@uri_option
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


@click.group()
@uri_option
def versions_cli(uri):
    """ List, export and import bgexplorer versions """
    mongoengine.connect(host=uri)


@versions_cli.command('list')
def list_versions():
    """ List the versions """
    for settings in vc.list_versions():
        kind = 'branch' if settings.editable else 'tag'
        click.echo(f"{settings.version_tag}\t{kind}\t"
                   f"{settings.description or ''}")


@versions_cli.command('export')
@click.argument('name')
@click.option('-o', '--output', type=click.Path(dir_okay=False,
                                                allow_dash=True),
              help="File to write, '-' for stdout  [default: "
                   "NAME.bgx.tar.gz]")
def export(name, output):
    """ Export version NAME to a file """
    if not vc.version_exists(name):
        raise click.ClickException(f"No version named '{name}'")
    output = output or f"{name}.bgx.tar.gz"
    with click.open_file(output, 'wb') as out:
        manifest = export_version(name, out)
    counts = ', '.join(f"{n} {cls}" for cls, n in manifest['counts'].items())
    # stdout may be the file itself
    click.echo(f"Exported '{name}' to {output}: {counts}", err=True)


@versions_cli.command('import')
@click.argument('file', type=click.File('rb'))
@click.option('--name', required=True, help="Name of the new version")
@click.option('--tag', is_flag=True, help="Import as a read-only tag")
@click.option('--description', help="Description of the new version")
def import_(file, name, tag, description):
    """ Import a version file as a new version """
    try:
        settings = import_version(file, name, editable=not tag,
                                  description=description)
    except KeyError:
        raise click.ClickException(f"There is already a version '{name}'")
    except VersionFileError as e:
        raise click.ClickException('\n  '.join([str(e)] + e.problems))
    except ValueError as e:
        raise click.ClickException(str(e))
    kind = 'branch' if settings.editable else 'tag'
    click.echo(f"Imported {kind} '{name}'")


@click.group()
@uri_option
def jobs_cli(uri):
    """ List, cancel and clear up background jobs """
    mongoengine.connect(host=uri)


@jobs_cli.command('list')
@click.option('--status', type=click.Choice(job_models.STATUSES))
@click.option('--kind', help="Only jobs of this kind")
@click.option('--version', help="Only jobs for this version")
@click.option('--user', help="Only jobs started by this user")
@click.option('--limit', type=click.IntRange(min=1), default=50,
              show_default=True)
def list_jobs(status, kind, version, user, limit):
    """ List the latest jobs, newest first """
    for job in job_maintenance.find_jobs(status=status, kind=kind,
                                         scope=version, user=user,
                                         limit=limit):
        click.echo('\t'.join([
            str(job.id), job.status, job.kind, job.scope or '-',
            job.user or '-', job.created.strftime('%Y-%m-%d %H:%M:%S'),
            (job.error or '').splitlines()[0] if job.error else '']))


@jobs_cli.command('cancel')
@click.argument('job_id')
def cancel_job(job_id):
    """ Cancel a queued job, or ask a running one to stop """
    outcome = job_maintenance.cancel_job(job_id)
    if outcome is None:
        raise click.ClickException(f"No job with id {job_id}")
    if outcome == 'finished':
        raise click.ClickException("The job has already ended")
    click.echo("Job cancelled" if outcome == 'cancelled' else
               "Asked the job to stop at its next safe point")


@jobs_cli.command('abandon')
@click.argument('job_id')
def abandon_job(job_id):
    """ Give up on a running job that is stuck, so that it no longer holds
    up the jobs behind it. Whatever is running it isn't stopped, and any
    version lock it holds stays; see `bgexplorer-versions`. """
    if job_maintenance.abandon_job(job_id):
        click.echo("Job abandoned")
    elif job_models.get(job_id) is None:
        raise click.ClickException(f"No job with id {job_id}")
    else:
        raise click.ClickException("The job is not running")


@jobs_cli.command('clear')
@click.option('--all', 'everything', is_flag=True,
              help="Also remove the jobs that are waiting to start")
@click.option('--files-dir', envvar='FLASK_JOBS_FILES_DIR',
              default=job_maintenance.default_files_dir, show_default=True,
              help="Where jobs keep files, which are removed with their "
                   "jobs [env var: FLASK_JOBS_FILES_DIR]")
def clear_jobs(everything, files_dir):
    """ Remove finished jobs, which are cleared after a few days anyway """
    cleared = job_maintenance.clear_jobs(finished_only=not everything,
                                         files_dir=files_dir)
    click.echo(f"Removed {len(cleared)} job{'' if len(cleared) == 1 else 's'}")


def main():
    cli()


def versions_main():
    versions_cli()


def jobs_main():
    jobs_cli()


if __name__ == '__main__':
    main()
