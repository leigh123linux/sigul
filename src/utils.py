# Copyright (C) 2008, 2009 Red Hat, Inc.  All rights reserved.
#
# This copyrighted material is made available to anyone wishing to use, modify,
# copy, or redistribute it subject to the terms and conditions of the GNU
# General Public License v.2.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY expressed or implied, including the
# implied warranties of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.  You should have
# received a copy of the GNU General Public License along with this program; if
# not, write to the Free Software Foundation, Inc., 51 Franklin Street, Fifth
# Floor, Boston, MA 02110-1301, USA.  Any Red Hat trademarks that are
# incorporated in the source code or documentation are not subject to the GNU
# General Public License and may only be used or replicated with the express
# permission of Red Hat, Inc.
#
# Red Hat Author: Miloslav Trmac <mitr@redhat.com>

import ConfigParser
import datetime
import errno
import getpass
import grp
import logging
import optparse
import os
import pwd
import re
import stat
import struct
import sys
import tempfile
import xmlrpclib

import M2Crypto.EVP
import nss.error
import nss.nss
import nss.ssl

import settings

 # Configuration handling

class ConfigurationError(Exception):
    '''Error reading utility configuration.'''
    pass

class Configuration(object):
    '''A configuration of one of the utilities.'''

    default_config_file = None

    def __init__(self, config_file, **unused_kwargs):
        '''Read config_file, overriding data in self.default_config_file.

        Raise ConfigurationError.

        '''
        defaults = {}
        self._add_defaults(defaults)
        parser = ConfigParser.RawConfigParser(defaults)
        config_paths = (os.path.join(settings.config_dir,
                                     self.default_config_file),
                        os.path.expanduser(config_file))
        files = parser.read(config_paths)
        if len(files) == 0:
            raise ConfigurationError('No configuration file found (tried %s)' %
                                     ', '.join("'%s'" % p
                                               for p in config_paths))
        try:
            self._read_configuration(parser)
        # ValueError is not handled by parser.getint()
        except (ConfigParser.Error, ValueError), e:
            raise ConfigurationError('Error reading configuration: %s' % str(e))

    def _add_defaults(self, defaults):
        '''Add more default values to defaults.'''
        pass

    def _read_configuration(self, parser):
        '''Set attributes depending on parser.

        Raise ConfigParser.Error.

        '''
        pass

def optparse_add_config_file_option(parser, default):
    '''Add a --config-file option to parser with the specified default.'''
    parser.add_option('-c', '--config-file', help='Configuration file path')
    parser.set_defaults(config_file=default)

def optparse_add_verbosity_option(parser):
    '''Add --verbose option to parser.'''
    parser.add_option('-v', '--verbose', action='count',
                      help='More verbose output (twice for debugging messages)')

def logging_level_from_options(options):
    '''Return a logging verbosity level depending on options.verbose'''
    if options.verbose <= 0:
        return logging.WARNING
    elif options.verbose == 1:
        return logging.INFO
    else: # options.verbose >= 2
        return logging.DEBUG

def get_daemon_options(description, default_config_file, daemonize=True):
    '''Handle command-line options for a daemon.

    Return the options object.  Add '-d', '--daemonize' if daemonize.  Exit on
    error.

    '''
    parser = optparse.OptionParser(usage='%prog [options]',
                                   version='%%prog %s' % (settings.version),
                                   description=description)
    optparse_add_config_file_option(parser, default_config_file)
    optparse_add_verbosity_option(parser)
    if daemonize:
        parser.add_option('-d', '--daemonize', action='store_true',
                          help='Run in the background')
        parser.set_defaults(daemonize=False)
    (options, args) = parser.parse_args()
    if len(args) != 0:
        parser.error('unexpected argument')
    return options

 # Koji utilities

class KojiError(Exception):
    pass

_u8_format = '!B'
def u8_pack(v):
    return struct.pack(_u8_format, v)

def u8_unpack(bytes):
    return struct.unpack(_u8_format, bytes)[0]

u8_size = struct.calcsize(_u8_format)

_u32_format = '!I'
def u32_pack(v):
    return struct.pack(_u32_format, v)

def u32_unpack(bytes):
    return struct.unpack(_u32_format, bytes)[0]

u32_size = struct.calcsize(_u32_format)

def koji_read_config():
    '''Read koji's configuration and verify it.

    Return a dictionary of options.

    '''
    parser = ConfigParser.ConfigParser()
    # FIXME? make this configurable, handle user-specified config files
    parser.read(('/etc/koji.conf', os.path.expanduser('~/.koji/config')))
    config = dict(parser.items('koji'))
    for opt in ('server', 'cert', 'ca', 'serverca', 'pkgurl'):
        if opt not in config:
            raise KojiError('Missing koji configuration option %s' % opt)
    for opt in ('cert', 'ca', 'serverca'):
        config[opt] = os.path.expanduser(config[opt])
    return config

def koji_connect(koji_config, authenticate, proxyuser=None):
    '''Return an authenticated koji session.

    Authenticate as user, on behalf of proxyuser if not None.

    '''
    # Don't import koji at the top of the file!  The rpm Python module calls
    # NSS_NoDB_Init() during its initialization, which breaks our attempts to
    # initialize nss with our certificate database.
    import koji

    session = koji.ClientSession(koji_config['server'])
    if authenticate:
        session.ssl_login(koji_config['cert'], koji_config['ca'],
                          koji_config['serverca'], proxyuser=proxyuser)
    try:
        version = session.getAPIVersion()
    except xmlrpclib.ProtocolError:
        raise KojiError('Cannot connect to Koji')
    if version != koji.API_VERSION:
        raise KojiError('Koji API version mismatch (server %d, client %d)' %
                        (version, koji.API_VERSION))
    return session

def koji_disconnect(session):
    try:
        session.logout()
    except:
        pass

 # Crypto utilities

class NSSConfiguration(Configuration):

    def _add_defaults(self, defaults):
        super(NSSConfiguration, self)._add_defaults(defaults)
        defaults.update({'nss-dir': '~/.sigul', 'nss-password': None})

    def _read_configuration(self, parser):
        super(NSSConfiguration, self)._read_configuration(parser)
        self.nss_dir = os.path.expanduser(parser.get('nss', 'nss-dir'))
        if not os.path.isdir(self.nss_dir):
            raise ConfigurationError('[nss] nss-dir \'%s\' is not a directory' %
                                     self.nss_dir)
        self.nss_password = parser.get('nss', 'nss-password')
        if self.nss_password is None:
            self.nss_password = getpass.getpass('NSS database password: ')

def nss_client_auth_callback_single(unused_ca_names, cert):
    '''Provide the specified certificate.'''
    return (cert, nss.nss.find_key_by_any_cert(cert))

class NSSInitError(Exception):
    '''Error in nss_init.'''
    pass

def nss_init(config):
    '''Initialize NSS.

    Raise NSSInitError.

    '''
    def _password_callback(unused_slot, retry):
        if not retry:
            return config.nss_password
        return None

    nss.nss.set_password_callback(_password_callback)
    try:
        nss.ssl.nssinit(config.nss_dir)
    except nss.error.NSPRError, e:
        if e.errno == nss.error.SEC_ERROR_BAD_DATABASE:
            raise NSSInitError('\'%s\' does not contain a valid NSS database' %
                               (config.nss_dir,))
        raise
    nss.ssl.set_domestic_policy()
    nss.ssl.config_server_session_id_cache()

def md5_digest(data):
    '''Return a md5 digest of data.'''
    return str(nss.nss.md5_digest(buffer(data)))

def sha512_digest(data):
    '''Return a sha512 digest of data.'''
    return str(nss.nss.sha512_digest(buffer(data)))

class M2CryptoSHA512DigestMod(object):

    '''A hashlib-like wrapper for M2Crypto's SHA-512 implementation.'''
    # This is necessary only because Python 2.4 RHEL5 does not contain hashlib.

    @classmethod
    def new(cls, data=None):
        return cls(data)

    def __init__(self, data=None):
        self.__digest = M2Crypto.EVP.MessageDigest('sha512')
        if data is not None:
            self.update(data)

    def copy(self):
        # This is an ugly hack, handles only hmac.digest() and only assuming
        # hmac.digest() won't be called more than once.
        return self

    def digest(self):
        return self.__digest.final()

    def update(self, data):
        data = str(data) # M2Crypto hashes unicode objects differently
        self.__digest.update(data)

    digest_size = 64
    block_size = 128

 # Protocol utilities

protocol_version = 0

class InvalidFieldsError(Exception):
    pass

def read_fields(read_fn):
    '''Read field mapping using read_fn(bytes).

    Return field mapping.  Raise InvalidFieldsError on error.  read_fn(bytes)
    must return exactly bytes bytes.

    '''
    buf = read_fn(u8_size)
    num_fields = u8_unpack(buf)
    if num_fields > 255:
        raise InvalidFieldsError('Too many fields')
    fields = {}
    for _ in xrange(num_fields):
        buf = read_fn(u8_size)
        bytes = u8_unpack(buf)
        if bytes == 0 or bytes > 255:
            raise InvalidFieldsError('Invalid field key length')
        key = read_fn(bytes)
        if not string_is_safe(key):
            raise InvalidFieldsError('Unprintable key value')
        buf = read_fn(u8_size)
        bytes = u8_unpack(buf)
        if bytes > 255:
            raise InvalidFieldsError('Invalid field value length')
        value = read_fn(bytes)
        fields[key] = value
    return fields

def format_fields(fields):
    '''Return fields formated using the protocol.

    Raise ValueError on invalid values.

    '''
    if len(fields) > 255:
        raise ValueError('Too many fields')
    data = u8_pack(len(fields))
    for (key, value) in fields.iteritems():
        if len(key) > 255:
            raise ValueError('Key name %s too long' % key)
        data += u8_pack(len(key))
        data += key
        if isinstance(value, int):
            value = u32_pack(value)
        elif isinstance(value, bool):
            if value:
                value = u32_pack(1)
            else:
                value = u32_pack(0)
        elif not isinstance(value, str):
            raise ValueError('Unknown value type of %s' % repr(value))
        if len(value) > 255:
            raise ValueError('Value %s too long' % repr(value))
        data += u8_pack(len(value))
        data += value
    return data

def string_is_safe(s):
    '''Return True if s an allowed readable string.'''
    # Motivated by 100% readable logs
    for c in s:
        if ord(c) < 0x20 or ord(c) > 0x7F:
            return False
    return True

_date_re = re.compile('^\d\d\d\d-\d\d-\d\d$')
def yyyy_mm_dd_is_valid(s):
    '''Return True if s is a valid yyyy-mm-dd date.'''
    if _date_re.match(s) is None:
        return False
    try:
        datetime.date(int(s[:4]), int(s[5:7]), int(s[8:]))
    except ValueError:
        return False
    return True

 # Utilities for daemons

class DaemonIDConfiguration(Configuration):
    '''UID/GID configuration for a daemon.'''

    def _read_configuration(self, parser):
        super(DaemonIDConfiguration, self)._read_configuration(parser)
        user = parser.get('daemon', 'unix-user')
        if user is not None:
            try:
                user = pwd.getpwnam(user).pw_uid
            except KeyError:
                try:
                    user = int(user)
                except ValueError:
                    raise ConfigurationError('[daemon] unix-user \'%s\' not '
                                             'found' %
                                        user)
        self.daemon_uid = user
        group = parser.get('daemon', 'unix-group')
        if group is not None:
            try:
                group = grp.getgrnam(group).gr_gid
            except KeyError:
                try:
                    group = int(group)
                except ValueError:
                    raise ConfigurationError('[daemon] unix-group \'%s\' not '
                                             'found' % group)
        self.daemon_gid = group

def set_regid(config):
    '''Change real and effective GID according to config.'''
    if config.daemon_gid is not None:
        try:
            os.setregid(config.daemon_gid, config.daemon_gid)
        except:
            logging.error('Error switching to group %d: %s', config.daemon_gid,
                          sys.exc_info()[1])
            raise

def set_reuid(config):
    '''Change real and effective UID according to config.'''
    if config.daemon_uid is not None:
        try:
            os.setreuid(config.daemon_uid, config.daemon_uid)
        except:
            logging.error('Error switching to user %d: %s', config.daemon_uid,
                          sys.exc_info()[1])
            raise

def update_HOME_for_uid(config):
    '''Update $HOME for config.daemon_uid if necessary.'''
    if config.daemon_uid is not None:
        os.environ['HOME'] = pwd.getpwuid(config.daemon_uid).pw_dir

def daemonize():
    '''Fork and terminate the parent, prepare the child to run as a daemon.'''
    if os.fork() != 0:
        logging.shutdown()
        os._exit(0)
    os.setsid()
    os.chdir('/')
    try:
        fd = os.open('/dev/null', os.O_RDWR)
    except OSError:
        pass
    else:
        try:
            os.dup2(fd, 0)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
        finally:
            if fd > 2:
                try:
                    os.close(fd)
                except OSError:
                    pass

def create_pid_file(daemon_name):
    '''Create a PID file with the specified name.'''
    f = open(os.path.join(settings.pid_dir, daemon_name + '.pid'), 'w')
    try:
        f.write('%s\n' % os.getpid())
    finally:
        f.close()

def delete_pid_file(daemon_name):
    '''Create a PID file with the specified name.'''
    os.remove(os.path.join(settings.pid_dir, daemon_name + '.pid'))

def sigterm_handler(*unused_args):
    sys.exit(0) # "raise SystemExit..."

 # Miscellaneous utilities

def write_new_file(path, writer_fn):
    '''Atomically replace file at path with data written by writer_fn(fd).'''
    (dirname, basename) = os.path.split(path)
    (fd, tmp_path) = tempfile.mkstemp(prefix=basename, dir=dirname)
    remove_tmp_path = True
    f = None
    try:
        f = os.fdopen(fd, 'w')
        writer_fn(f)
        try:
            st = os.stat(path)
        except OSError, e:
            if e.errno != errno.ENOENT:
                raise
        else:
            # fchmod is unfortunately not available
            os.chmod(tmp_path,
                     st.st_mode & (stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO))
        f.close()
        f = None
        backup_path = path + '~'
        try:
            os.remove(backup_path)
        except OSError:
            pass
        try:
            os.link(path, backup_path)
        except OSError, e:
            if e.errno != errno.ENOENT:
                raise
        os.rename(tmp_path, path)
        remove_tmp_path = False
    finally:
        if f:
            f.close()
        if remove_tmp_path:
            os.remove(tmp_path)
