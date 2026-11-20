import os
import time

try:
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from ryu.ofproto import ofproto_v1_3
    from ryu.lib.packet import packet, ipv4, udp
    BaseApp = app_manager.RyuApp
except ImportError:
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
    from os_ken.ofproto import ofproto_v1_3
    from os_ken.lib.packet import packet, ipv4, udp
    BaseApp = app_manager.OSKenApp

H1 = '10.0.0.1'
H2 = '10.0.0.2'
H3 = '10.0.0.3'
CLIENTS = [H1, H3]
ALERT_PORT = 9999
LOG_FILE = 'controller_events.csv'

PRIMARY_PORTS = {(1, 3), (2, 1), (2, 2), (4, 1)}

PATHS = {
    'primary': {1: 3, 4: 1},
    'backup': {1: 4, 4: 2},
}


class FailoverController(BaseApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.static = os.environ.get('STATIC') == '1'
        self.dps = {}
        self.down_ports = set()
        self.path = 'primary'
        with open(LOG_FILE, 'w') as f:
            f.write('time,event,detail,path\n')
        mode = 'STATIC (no rerouting)' if self.static else 'ADAPTIVE (rerouting on)'
        self.log_event('start', mode)

    def log_event(self, event, detail=''):
        t = time.time()
        print('[%.3f] %s %s | path=%s' % (t, event, detail, self.path), flush=True)
        with open(LOG_FILE, 'a') as f:
            f.write('%.6f,%s,%s,%s\n' % (t, event, detail, self.path))

    def add_flow(self, dp, priority, match, actions):
        ofp = dp.ofproto
        parser = dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority,
                                      match=match, instructions=inst))

    def route(self, dp, dst, out_port):
        parser = dp.ofproto_parser
        match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=dst)
        self.add_flow(dp, 10, match, [parser.OFPActionOutput(out_port)])

    def install_switch(self, dp):
        dpid = dp.id
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        if dpid == 1:
            self.route(dp, H1, 1)
            self.route(dp, H3, 2)
            self.route(dp, H2, PATHS[self.path][1])
        elif dpid == 4:
            self.route(dp, H2, 3)
            for c in CLIENTS:
                self.route(dp, c, PATHS[self.path][4])
        elif dpid in (2, 3):
            self.route(dp, H2, 2)
            for c in CLIENTS:
                self.route(dp, c, 1)
        if dpid in (1, 4):
            match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, udp_dst=ALERT_PORT)
            actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
            self.add_flow(dp, 100, match, actions)

    def set_path(self, new_path, reason):
        if self.static:
            self.log_event('reroute_skipped', reason)
            return
        if new_path == self.path:
            return
        start = time.time()
        self.path = new_path
        for dpid in (1, 4):
            if dpid in self.dps:
                self.install_switch(self.dps[dpid])
        ms = (time.time() - start) * 1000
        self.log_event('reroute', '%s -> %s in %.2f ms' % (reason, new_path, ms))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def on_switch_connect(self, ev):
        dp = ev.msg.datapath
        self.dps[dp.id] = dp
        self.install_switch(dp)
        self.log_event('switch_connected', 's%d' % dp.id)

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def on_port_status(self, ev):
        msg = ev.msg
        ofp = msg.datapath.ofproto
        dpid = msg.datapath.id
        port = msg.desc.port_no
        down = bool(msg.desc.state & ofp.OFPPS_LINK_DOWN) or bool(msg.desc.config & ofp.OFPPC_PORT_DOWN)
        key = (dpid, port)
        if down and key not in self.down_ports:
            self.down_ports.add(key)
            self.log_event('port_down', 's%d-port%d' % key)
        elif not down and key in self.down_ports:
            self.down_ports.discard(key)
            self.log_event('port_up', 's%d-port%d' % key)
        else:
            return
        if key not in PRIMARY_PORTS:
            return
        if self.down_ports & PRIMARY_PORTS:
            self.set_path('backup', 'link_down')
        else:
            self.set_path('primary', 'link_restored')

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def on_packet_in(self, ev):
        pkt = packet.Packet(ev.msg.data)
        ip = pkt.get_protocol(ipv4.ipv4)
        u = pkt.get_protocol(udp.udp)
        if ip is None or u is None or u.dst_port != ALERT_PORT:
            return
        self.log_event('app_alert', 'from %s' % ip.src)
        self.set_path('backup', 'app_alert')
