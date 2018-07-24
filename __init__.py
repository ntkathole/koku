#
# Copyright 2018 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""App factory for Masu application."""
import errno
import logging
import os

from flask import Flask
from flask.logging import default_handler
from flask_sqlalchemy import SQLAlchemy

from masu.api.blueprint import api_v1
from masu.api.status import ApplicationStatus
from masu.celery import celery as celery_app, update_celery_config

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
logger.addHandler(default_handler)


def create_app(test_config=None):
    """
    App factory for Flask application.

    Args:
        test_config (dict): A mapping of configurations used for testing

    Returns:
        flask.app.Flask: The configured Flask application

    """
    app = Flask(__name__, instance_relative_config=True)

    # Load configs
    if test_config is None:
        app.config.from_object('masu.config.Config')
    else:
        app.config.from_mapping(test_config)

    # Logging
    logger.setLevel(app.config.get('LOG_LEVEL', 'WARNING'))

    try:
        os.makedirs(app.instance_path)
    # pylint: disable=invalid-name
    except OSError as e:
        # ignore "File exists"
        if e.errno != errno.EEXIST:
            logger.warning(e)

    # Establish database
    # pylint: disable=invalid-name, unused-variable
    db = SQLAlchemy(app)  # noqa: F841

    # Add application config to Celery
    update_celery_config(celery_app, app)

    # Blueprints
    app.register_blueprint(api_v1)

    ApplicationStatus().startup()

    return app
