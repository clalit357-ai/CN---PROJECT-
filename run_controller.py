import sys
from os_ken import cfg
from os_ken.base.app_manager import AppManager
from os_ken.lib import hub

cfg.CONF([], project='os_ken')
mgr = AppManager.get_instance()
mgr.load_apps([sys.argv[1] if len(sys.argv) > 1 else 'controller.py'])
services = mgr.instantiate_apps(**mgr.create_contexts())
hub.joinall(services)
