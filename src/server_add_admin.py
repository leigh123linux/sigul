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

import logging
import sys

import server_common
import utils


class AddAdminConfiguration(server_common.ServerBaseConfiguration):

    def _read_configuration(self, parser):
        super(AddAdminConfiguration, self)._read_configuration(parser)
        self.batch_mode = False


def main():
    parser = utils.create_basic_parser('Add an administrator to the signing '
                                       'server', '~/.sigul/server.conf')
    utils.optparse_add_batch_option(parser)
    parser.add_option('-n', '--name', metavar='USER',
                      help='Administrator user name')
    options = utils.optparse_parse_options_only(parser)

    logging.basicConfig(format='%(levelname)s: %(message)s',
                        level=utils.logging_level_from_options(options))
    try:
        config = AddAdminConfiguration(options.config_file)
    except utils.ConfigurationError as e:
        sys.exit(str(e))
    config.batch_mode = options.batch
    try:
        utils.set_regid(config)
        utils.set_reuid(config)
        utils.update_HOME_for_uid(config)
    except:
        # The failing function has already logged the exception
        sys.exit(1)

    try:
        utils.nss_init(config)
    except utils.NSSInitError as e:
        sys.exit(str(e))

    if options.name is not None:
        name = options.name
    else:
        # readline import makes raw_input more usable.  Import only here to
        # avoid sending escape sequences to stdout when not interactive.
        import readline
        name = utils.input('Administrator user name: ')

    password = utils.read_password(config, 'Administrator password: ')
    if not config.batch_mode:
        p2 = utils.read_password(config, 'Administrator password (again): ')
        if password != p2:
            sys.exit('Passwords don\'t match.')

    name = name.encode('utf-8')
    db = server_common.db_open(config)
    user = server_common.User(name, clear_password=password, admin=True)
    db.add(user)
    db.commit()

if __name__ == '__main__':
    main()
