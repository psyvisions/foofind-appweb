# -*- coding: utf-8 -*-
"""
    Módulo principal de la aplicación Web
"""
import foofind.globals

import os, os.path
from foofind import defaults
from collections import OrderedDict
from flask import Flask, g, request, render_template, redirect, abort, url_for, make_response, get_flashed_messages
from flask.ext.assets import Environment, Bundle
from flask.ext.babel import get_translations, gettext as _
from flask.ext.login import current_user
from babel import support, localedata, Locale
from raven.contrib.flask import Sentry
from webassets.filter import register_filter
from hashlib import md5

from foofind.web import allerrors

from foofind.services import *
from foofind.utils.webassets_filters import JsSlimmer, CssSlimmer
from foofind.utils import u, logging
from foofind.utils.bots import is_search_bot, is_full_browser, check_rate_limit

from appweb.blueprints.files import files
from appweb.templates import register_filters

def create_app(config=None, debug=False):
    '''
    Inicializa la aplicación Flask. Carga los siguientes módulos:
     - index: página de inicio
     - page: páginas estáticas
     - user: gestión del usuario
     - files: búsqueda y obtención de ficheros
     - status: servicio de monitorización de la aplicación

    Y además, inicializa los siguientes servicios:
     - Configuración: carga valores por defecto y modifica con el @param config
     - Web Assets: compilación y compresión de recursos estáticos
     - i18n: detección de idioma en la URL y catálogos de mensajes
     - Cache y auth: Declarados en el módulo services
     - Files: Clases para acceso a datos
    '''
    app = Flask(__name__)
    app.config.from_object(defaults)
    app.debug = debug

    # Configuración
    if config:
        app.config.from_object(config)

    # Gestión centralizada de errores
    if app.config["SENTRY_DSN"]:
        sentry.init_app(app)
    logging.getLogger().setLevel(logging.DEBUG if debug else logging.INFO)

    # Configuración dependiente de la versión del código
    revision_filename_path = os.path.join(os.path.dirname(app.root_path), "revision")
    if os.path.exists(revision_filename_path):
        f = open(revision_filename_path, "r")
        data = f.read()
        f.close()
        revisions = tuple(
            tuple(i.strip() for i in line.split("#")[0].split())
            for line in data.strip().split("\n")
            if line.strip() and not line.strip().startswith("#"))
        revision_hash = md5(data).hexdigest()
        app.config.update(
            CACHE_KEY_PREFIX = "%s%s/" % (
                app.config["CACHE_KEY_PREFIX"] if "CACHE_KEY_PREFIX" in app.config else "",
                revision_hash
                ),
            REVISION_HASH = revision_hash,
            REVISION = revisions
            )
    else:
        app.config.update(
            REVISION_HASH = None,
            REVISION = ()
            )

    # Registra filtros de plantillas
    register_filters(app)

    # Registra valores/funciones para plantillas
    app.jinja_env.globals["u"] = u

    # Blueprints
    app.register_blueprint(files)

    # Web Assets
    if not os.path.isdir(app.static_folder+"/gen"): os.mkdir(app.static_folder+"/gen")
    assets = Environment(app)
    app.assets = assets
    assets.debug = app.debug
    assets.versions = "timestamp"

    register_filter(JsSlimmer)
    register_filter(CssSlimmer)

    assets.register('css_blubster', Bundle('blubster/css/blubster.css', filters='pyscss', output='gen/main.css', debug=False), filters='css_slimmer', output='gen/blubster.css')
    assets.register('css_foofind', Bundle('foofind/css/foofind.css', filters='pyscss', output='gen/main.css', debug=False), filters='css_slimmer', output='gen/foofind.css')
    assets.register('js_appweb', Bundle('jquery.js', 'appweb.js', filters='rjsmin', output='gen/appweb.js'), )

    # proteccion CSRF
    csrf.init_app(app)

    # Traducciones
    babel.init_app(app)

    @babel.localeselector
    def get_locale():
        return "en"

    # Cache
    cache.init_app(app)

    # Acceso a bases de datos
    filesdb.init_app(app)
    feedbackdb.init_app(app)
    entitiesdb.init_app(app)

    # Servicio de búsqueda
    @app.before_first_request
    def init_process():
        if not eventmanager.is_alive():
            # Fallback inicio del eventManager
            eventmanager.start()

    # Profiler
    profiler.init_app(app, feedbackdb)

    eventmanager.once(searchd.init_app, hargs=(app, filesdb, entitiesdb, profiler))

    # Refresco de conexiones
    eventmanager.once(filesdb.load_servers_conn)
    eventmanager.interval(app.config["FOOCONN_UPDATE_INTERVAL"], filesdb.load_servers_conn)
    eventmanager.interval(app.config["FOOCONN_UPDATE_INTERVAL"], entitiesdb.connect)

    @app.url_value_preprocessor
    def pull_lang_code(endpoint, values):
        if values is None:
            g.lang = g.license_name = None
        else:
            g.lang = values.pop('lang', None)
            g.license_name = values.pop('license', None)

    @app.url_defaults
    def add_language_code(endpoint, values):
        if not 'lang' in values and app.url_map.is_endpoint_expecting(endpoint, 'lang'):
            values['lang'] = g.lang
        if not 'license' in values and app.url_map.is_endpoint_expecting(endpoint, 'license'):
            values['license'] = g.license_name

    @app.before_request
    def before_request():

        # No preprocesamos la peticiones a static
        if request.path.startswith("/static/"):
            return

        init_g(app)

        # ignora peticiones sin blueprint
        if request.blueprint is None and request.path.endswith("/"):
            if "?" in request.url:
                root = request.url_root[:-1]
                path = request.path.rstrip("/")
                query = request.url.decode("utf-8")
                query = query[query.find(u"?"):]
                return redirect(root+path+query, 301)
            return redirect(request.url.rstrip("/"), 301)


    @app.after_request
    def after_request(response):
        response.headers["X-UA-Compatible"] = "IE-edge"
        return response

    # Páginas de error
    errors = {
        404: ("Page not found", "The requested address does not exists."),
        410: ("Page not available", "The requested address is no longer available."),
        500: ("An error happened", "We had some problems displaying this page. Maybe later we can show it to you."),
        503: ("Service unavailable", "This page is temporarily unavailable. Please try again later.")
    }

    @allerrors(app, 400, 401, 403, 404, 405, 408, 409, 410, 411, 412, 413, 414, 415, 416, 417, 418, 500, 501, 502, 503)
    def all_errors(e):
        error = e.code if hasattr(e,"code") else 500
        title, description = errors[error if error in errors else 500]

        init_g(app)

        return render_template('error.html', code=str(error), title=title, description=description), error

    return app

def init_g(app):
    # caracteristicas del cliente
    g.search_bot=is_search_bot()

    # peticiones en modo preproduccion
    g.beta_request = request.url_root[request.url_root.index("//")+2:].startswith("beta.")

    # prefijo para los contenidos estáticos
    if g.beta_request:
        app_static_prefix = app.static_url_path
    else:
        app_static_prefix = app.config["STATIC_PREFIX"] or app.static_url_path
    g.static_prefix = app.assets.url = app_static_prefix

    g.keywords = set()
    g.args = {}

    g.page_description=g.title=""