import sys
import re
import time
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info


class Diamond(Topo):
    def build(self):
        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
        h3 = self.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')
        s3 = self.addSwitch('s3', protocols='OpenFlow13')
        s4 = self.addSwitch('s4', protocols='OpenFlow13')
        self.addLink(h1, s1, port2=1)
        self.addLink(h3, s1, port2=2)
        self.addLink(s1, s2, port1=3, port2=1)
        self.addLink(s1, s3, port1=4, port2=1)
        self.addLink(s2, s4, port1=2, port2=1)
        self.addLink(s3, s4, port1=2, port2=2)
        self.addLink(h2, s4, port2=3)


def build_net():
    net = Mininet(topo=Diamond(), controller=None, switch=OVSSwitch,
                  autoStaticArp=True)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    return net


def failover_test(net):
    h1 = net.get('h1')
    info('*** waiting for controller to install flows\n')
    time.sleep(3)
    info('*** pinging h2 from h1 every 100ms for 15s\n')
    h1.cmd('ping -D -i 0.1 -c 150 10.0.0.2 > /tmp/failover_ping.txt 2>&1 &')
    time.sleep(5)
    info('*** t=5s  link s1-s2 DOWN\n')
    net.configLinkStatus('s1', 's2', 'down')
    time.sleep(5)
    info('*** t=10s link s1-s2 UP\n')
    net.configLinkStatus('s1', 's2', 'up')
    time.sleep(6)
    out = open('/tmp/failover_ping.txt').read()
    seqs = sorted(int(s) for s in re.findall(r'icmp_seq=(\d+)', out))
    lost = sorted(set(range(1, 151)) - set(seqs))
    info('*** replies received: %d / 150\n' % len(seqs))
    info('*** lost seq numbers: %s\n' % lost)
    info('*** approx outage: %d ms\n' % (len(lost) * 100))
    info('*** full ping output saved at /tmp/failover_ping.txt\n')


if __name__ == '__main__':
    setLogLevel('info')
    net = build_net()
    net.start()
    if len(sys.argv) > 1 and sys.argv[1] == 'test':
        failover_test(net)
    else:
        CLI(net)
    net.stop()
