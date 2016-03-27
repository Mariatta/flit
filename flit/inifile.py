import configparser
import difflib
import logging
import os
from pathlib import Path
import sys

import requests
import toml

from .vendorized.readme.rst import render
import io

from . import common

log = logging.getLogger(__name__)

class ConfigError(ValueError):
    pass

metadata_list_fields = {
    'classifiers',
    'requires',
    'dev-requires'
}

metadata_allowed_fields = {
    'module',
    'author',
    'author-email',
    'maintainer',
    'maintainer-email',
    'home-page',
    'license',
    'keywords',
    'requires-python',
    'dist-name',
    'entry-points-file',
} | metadata_list_fields

metadata_required_fields = {
    'module',
    'author',
    'author-email',
    'home-page',
}

def get_cache_dir():
    if os.name == 'posix' and sys.platform != 'darwin':
        # Linux, Unix, AIX, etc.
        # use ~/.cache if empty OR not set
        xdg = os.environ.get("XDG_CACHE_HOME", None) or (os.path.expanduser('~/.cache'))
        return Path(xdg, 'flit')

    elif sys.platform == 'darwin':
        return Path(os.path.expanduser('~'), 'Library/Caches/flit')

    else:
        # Windows (hopefully)
        local = os.environ.get('LOCALAPPDATA', None) or (os.path.expanduser('~\\AppData\\Local'))
        return Path(local, 'flit')

def _verify_classifiers_cached(classifiers):
    with (get_cache_dir() / 'classifiers.lst').open() as f:
        valid_classifiers = set(l.strip() for l in f)

    invalid = classifiers - valid_classifiers
    if invalid:
        raise ConfigError("Invalid classifiers:\n" +
                          "\n".join(invalid))

def _download_classifiers():
    log.info('Fetching list of valid trove classifiers')
    resp = requests.get('https://pypi.python.org/pypi?%3Aaction=list_classifiers')
    resp.raise_for_status()

    cache_dir = get_cache_dir()
    try:
        cache_dir.mkdir(parents=True)
    except FileExistsError:
        pass
    with (get_cache_dir() / 'classifiers.lst').open('wb') as f:
        f.write(resp.content)

def verify_classifiers(classifiers):
    classifiers = set(classifiers)
    try:
        _verify_classifiers_cached(classifiers)
    except (FileNotFoundError, ConfigError) as e1:
        # FileNotFoundError: We haven't yet got the classifiers cached
        # ConfigError: At least one is invalid, but it may have been added since
        #   last time we fetched them.

        if os.environ.get('FLIT_NO_NETWORK', ''):
            log.warn("Not checking classifiers, because FLIT_NO_NETWORK is set")
            return

        # Try to download up-to-date list of classifiers
        try:
            _download_classifiers()
        except requests.ConnectionError:
            # The error you get on a train, going through Oregon, without wifi
            if isinstance(e1, ConfigError):
                raise e1
            else:
                log.warn("Couldn't get list of valid classifiers to check against")
        else:
            _verify_classifiers_cached(classifiers)


def read_pkg_ini(path: Path):
    """Read and check the `flit.toml` or `flit.ini` file with data about the package.
    """
    if path.suffix == '.toml':
        with path.open() as f:
            d = toml.load(f)
        return prep_toml_config(d, path)
    else:
        # Treat all other extensions as the older flit.ini format
        cp = _read_pkg_ini(path)
        return _validate_config(cp, path)

class EntryPointsConflict(ValueError):
    def __str__(self):
        return ('Please specify console_scripts entry points, or [scripts] in '
            'flit config, not both.')

def prep_toml_config(d, path):
    unknown_sections = set(d) - {'metadata', 'scripts', 'entrypoints'}
    unknown_sections = [s for s in unknown_sections if not s.lower().startswith('x-')]
    if unknown_sections:
        raise ConfigError('Unknown sections: ' + ', '.join(unknown_sections))

    if 'metadata' not in d:
        raise ConfigError('[metadata] section is required')

    md_dict, module = _prep_metadata(d['metadata'], path)

    if 'scripts' in d:
        scripts_dict = {k: common.parse_entry_point(v) for k, v in d['scripts'].items()}
    else:
        scripts_dict = {}

    if 'entrypoints' in d:
        entrypoints = flatten_entrypoints(d['entrypoints'])
    else:
        entrypoints = {}
    _add_scripts_to_entrypoints(entrypoints, scripts_dict)

    return {
        'module': module,
        'metadata': md_dict,
        'scripts': scripts_dict,
        'entrypoints': entrypoints,
        'raw_config': d,
    }

def flatten_entrypoints(ep):
    """Flatten nested entrypoints dicts.

    Entry points group names can include dots. But dots in TOML make nested
    dictionaries:

    [entrypoints.a.b]    # {'entrypoints': {'a': {'b': {}}}}

    The proper way to avoid this is:

    [entrypoints."a.b"]  # {'entrypoints': {'a.b': {}}}

    But since there isn't a need for arbitrarily nested mappings in entrypoints,
    flit allows you to use the former. This flattens the nested dictionaries
    from loading flit.toml.
    """
    def _flatten(d, prefix):
        d1 = {}
        for k, v in d.items():
            if isinstance(v, dict):
                yield from _flatten(v, prefix+'.'+k)
            else:
                d1[k] = v

        if d1:
            yield prefix, d1

    res = {}
    for k, v in ep.items():
        res.update(_flatten(v, k))
    return res

def _add_scripts_to_entrypoints(entrypoints, scripts_dict):
    if scripts_dict:
        if 'console_scripts' in entrypoints:
            raise EntryPointsConflict
        else:
            entrypoints['console_scripts'] = scripts_dict


def _read_pkg_ini(path):
    cp = configparser.ConfigParser()
    with path.open() as f:
        cp.read_file(f)

    return cp

def _prep_metadata(md_sect, path):
    if not set(md_sect).issuperset(metadata_required_fields):
        missing = metadata_required_fields - set(md_sect)
        raise ConfigError("Required fields missing: " + '\n'.join(missing))

    module = md_sect.get('module')
    if not module.isidentifier():
        raise ConfigError("Module name %r is not a valid identifier" % module)

    md_dict = {}

    if 'description-file' in md_sect:
        description_file = path.parent / md_sect.get('description-file')
        with description_file.open() as f:
            raw_desc =  f.read()
        if description_file.suffix == '.md':
            try:
                import pypandoc
                log.debug('will convert %s to rst', description_file)
                raw_desc = pypandoc.convert(raw_desc, 'rst', format='markdown')
            except Exception:
                log.warn('Unable to convert markdown to rst. Please install `pypandoc` and `pandoc` to use markdown long description.')
        stream = io.StringIO()
        _, ok = render(raw_desc, stream)
        if not ok:
            log.warn("The file description seems not to be valid rst for PyPI;"
                    " it will be interpreted as plain text")
            log.warn(stream.getvalue())
        md_dict['description'] =  raw_desc

    for key, value in md_sect.items():
        if key in {'description-file', 'module'}:
            continue
        if key not in metadata_allowed_fields:
            closest = difflib.get_close_matches(key, metadata_allowed_fields,
                                                n=1, cutoff=0.7)
            msg = "Unrecognised metadata key: {}".format(key)
            if closest:
                msg += " (did you mean {!r}?)".format(closest[0])
            raise ConfigError(msg)

        k2 = key.replace('-', '_')
        md_dict[k2] = value
        if key in metadata_list_fields:
            if not isinstance(value, list):
                raise ConfigError('Expected a list for {} field, found {!r}'
                                    .format(key, value))
            if not all(isinstance(a, str) for a in value):
                raise ConfigError('Expected a list of strings for {} field'
                                    .format(key))
        else:
            if not isinstance(value, str):
                raise ConfigError('Expected a string for {} field, found {!r}'
                                    .format(key, value))

    return md_dict, module

def _validate_config(cp, path):
    """
    Validate a config and return a dict containing `module`,`metadata`,`script`,`entry_point` keys.
    """
    unknown_sections = set(cp.sections()) - {'metadata', 'scripts'}
    unknown_sections = [s for s in unknown_sections if not s.lower().startswith('x-')]
    if unknown_sections:
        raise ConfigError('Unknown sections: ' + ', '.join(unknown_sections))

    if not cp.has_section('metadata'):
        raise ConfigError('[metadata] section is required')

    md_sect = {}
    for k, v in cp['metadata'].items():
        if k in metadata_list_fields:
            md_sect[k] = v.splitlines()
        else:
            md_sect[k] = v

    if 'entry-points-file' in md_sect:
        entry_points_file = path.parent / md_sect.pop('entry-points-file')
        if not entry_points_file.is_file():
            raise FileNotFoundError(entry_points_file)
    else:
        entry_points_file = path.parent / 'entry_points.txt'
        if not entry_points_file.is_file():
            entry_points_file = None

    if entry_points_file:
        ep_cp = configparser.ConfigParser()
        with entry_points_file.open() as f:
            ep_cp.read_file(f)
        # Convert to regular dict
        entrypoints = {k: dict(v) for k,v in ep_cp.items()}
    else:
        entrypoints = {}

    md_dict, module = _prep_metadata(md_sect, path)


    # What we call requires in the ini file is technically requires_dist in
    # the metadata.
    if 'requires' in md_dict:
        md_dict['requires_dist'] = md_dict.pop('requires')

    # And what we call dist-name is name in the metadata
    if 'dist_name' in md_dict:
        md_dict['name'] = md_dict.pop('dist_name')

    if 'classifiers' in md_dict:
        verify_classifiers(md_dict['classifiers'])

    # Scripts ---------------
    if cp.has_section('scripts'):
        scripts_dict = {k: common.parse_entry_point(v) for k, v in cp['scripts'].items()}
    else:
        scripts_dict = {}

    _add_scripts_to_entrypoints(entrypoints, scripts_dict)

    return {
        'module': module,
        'metadata': md_dict,
        'scripts': scripts_dict,
        'entrypoints': entrypoints,
        'raw_config': cp,
    }
