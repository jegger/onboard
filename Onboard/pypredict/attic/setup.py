#!/usr/bin/env python

# Copyright © 2009, 2012 marmuta <marmvta@gmail.com>
#
# This file is part of Onboard.
#
# Onboard is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# Onboard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import os
import glob
import sys
from distutils.core import setup, Extension
from os.path import join

try:
    import DistUtilsExtra.auto
except ImportError:
    print >> sys.stderr, 'To build project_name you need https://launchpad.net/python-distutils-extra'
    sys.exit(1)
assert DistUtilsExtra.auto.__version__ >= '2.18', 'needs DistUtilsExtra.auto >= 2.18'


class Extension_lm(Extension):
    sources = ['lm.cpp',
              'lm_dynamic.cpp',
              'lm_merged.cpp',
              'lm_python.cpp',
              'pool_allocator.cpp']

    depends = ['lm.h',
              'lm_dynamic.h',
              'lm_dynamic_impl.h',
              'lm_dynamic_kn.h',
              'lm_dynamic_cached.h',
              'lm_merged.h']

    def __init__(self, root = "", module_root = ""):
        path = join(root, 'pypredict', 'lm')
        sources = [join(path, x) for x in self.sources]

        module_name = "pypredict.lm"
        if module_root:
            module_name = module_root + "." + module_name

        Extension.__init__(self,
                           module_name,
                           sources = sources,
                           depends = self.depends,             
                           undef_macros = [],
                           library_dirs = [],
                           libraries = [],
                           #define_macros=[('NDEBUG', '1')], 
                          )

def setup_package():
    lm = Extension_lm()
    setup(name='locubus',
          version='0.5',
          description='Word Prediction D-Bus Service',
          author='marmuta',
          author_email='marmvta@gmail.com',
          license = 'gpl',
          url='http://launchpad.net/onboard',
          ext_package='pypredict.lm',
          ext_modules = [lm],
          packages=['pypredict', 'pypredict.lm'],
          data_files = [('share/locubus/models', glob.glob('models/*.lm'))],
          scripts = ['locubus'],
         )

if __name__ == '__main__':
    setup_package()
