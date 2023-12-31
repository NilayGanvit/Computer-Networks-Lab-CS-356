import json
import time
from random import randint

from graph import Graph
from ryu.app.wsgi import ControllerBase, Response, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import dpid as dpid_lib
from ryu.lib.packet import ethernet, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_host, get_link, get_switch


class TopologyController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(TopologyController, self).__init__(req, link, data, **config)
        self.topology_api_app = data["topology_api_app"]

    @route("topology", "/topology/switches", methods=["GET"])
    def list_switches(self, req, **kwargs):
        return self._switches(req, **kwargs)

    @route(
        "topology",
        "/topology/switches/{dpid}",
        methods=["GET"],
        requirements={"dpid": dpid_lib.DPID_PATTERN},
    )
    def get_switch(self, req, **kwargs):
        return self._switches(req, **kwargs)

    @route("topology", "/topology/links", methods=["GET"])
    def list_links(self, req, **kwargs):
        return self._links(req, **kwargs)

    @route(
        "topology",
        "/topology/links/{dpid}",
        methods=["GET"],
        requirements={"dpid": dpid_lib.DPID_PATTERN},
    )
    def get_links(self, req, **kwargs):
        return self._links(req, **kwargs)

    @route("topology", "/topology/hosts", methods=["GET"])
    def list_hosts(self, req, **kwargs):
        return self._hosts(req, **kwargs)

    @route(
        "topology",
        "/topology/hosts/{dpid}",
        methods=["GET"],
        requirements={"dpid": dpid_lib.DPID_PATTERN},
    )
    def get_hosts(self, req, **kwargs):
        return self._hosts(req, **kwargs)

    def _switches(self, req, **kwargs):
        dpid = None
        if "dpid" in kwargs:
            dpid = dpid_lib.str_to_dpid(kwargs["dpid"])
        switches = get_switch(self.topology_api_app, dpid)
        body = json.dumps([switch.to_dict() for switch in switches])
        return Response(content_type="application/json", body=body)

    def _links(self, req, **kwargs):
        dpid = None
        if "dpid" in kwargs:
            dpid = dpid_lib.str_to_dpid(kwargs["dpid"])
        links = get_link(self.topology_api_app, dpid)
        body = json.dumps([link.to_dict() for link in links])
        return Response(content_type="application/json", body=body)

    def _hosts(self, req, **kwargs):
        dpid = None
        if "dpid" in kwargs:
            dpid = dpid_lib.str_to_dpid(kwargs["dpid"])
        hosts = get_host(self.topology_api_app, dpid)
        body = json.dumps([host.to_dict() for host in hosts])
        return Response(content_type="application/json", body=body)


class SimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch13, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        wsgi = kwargs["wsgi"]
        wsgi.register(TopologyController, {"topology_api_app": self})

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)
        ]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                buffer_id=buffer_id,
                priority=priority,
                match=match,
                instructions=inst,
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                match=match,
                instructions=inst,
                cookie=randint(0, 255),
            )
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug(
                "packet truncated: only %s of %s bytes",
                ev.msg.msg_len,
                ev.msg.total_len,
            )
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        dst = eth.dst
        src = eth.src

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        # self.logger.info("\tpacket in %s %s %s %s", dpid, src, dst, in_port)
        # learn a mac address to avoid FLOOD next time.
        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # install a flow to avoid packet_in next time
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            # verify if we have a valid buffer_id, if yes avoid to send both
            # flow_mod & packet_out
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match, actions)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data,
        )
        datapath.send_msg(out)

    def get_topology(self):
        self.switches = get_switch(self)
        self.links = get_link(self)
        self.hosts = get_host(self)

        while len(self.switches) != len(self.hosts):
            time.sleep(0.05)
            print("\rLoading....")
            self.switches = get_switch(self)
            self.links = get_link(self)
            self.hosts = get_host(self)

    """
    The event EventSwitchEnter will trigger the activation of get_topology_data().
    """

    @set_ev_cls(event.EventSwitchEnter)
    def handler_switch_event(self, ev):
        print("New Switch", end="")
        self.get_topology()

        print("-------------------------------------------------")
        print("\nAll Links:")
        for l in self.links:
            print(l)

        print("\nAll Switches:")
        for s in self.switches:
            print(s)

        print("\nAll Hosts:")
        for h in self.hosts:
            print(h)

        self.graph = Graph(self.switches, self.hosts, self.links)

        print("\nFlows:")
        length = len(self.graph.switch_path)
        for switch in self.switches:
            data = switch.to_dict()
            src_id = int(data["dpid"]) - 1
            if src_id not in self.graph.switch_path:
                continue
            else:
                i = self.graph.switch_path.index(src_id)

            datapath = switch.dp
            ofp = datapath.ofproto
            parser = datapath.ofproto_parser


            if i == 0:
                in_port = 1
            else:
                in_port = self.graph.ports[src_id][self.graph.switch_path[i - 1]]

            if i == length - 1:
                out_port = 1
            else:
                out_port = self.graph.ports[src_id][self.graph.switch_path[i + 1]]

            print(f"Installing flows in switch with dpid {src_id+1}:")

            print(f"Match in_port: {in_port} and Action: {out_port}")
            match = parser.OFPMatch(in_port=in_port)
            actions = [parser.OFPActionOutput(out_port)]
            self.add_flow(datapath, 200, match, actions)

            print(f"Match in_port: {out_port} and Action: {in_port}")
            match = parser.OFPMatch(in_port=out_port)
            actions = [parser.OFPActionOutput(in_port)]
            self.add_flow(datapath, 200, match, actions)

    """
    This event is fired when a switch leaves the topo. i.e. fails.
    """

    @set_ev_cls(
        event.EventSwitchLeave, [MAIN_DISPATCHER, CONFIG_DISPATCHER, DEAD_DISPATCHER]
    )
    def handler_switch_leave(self, ev):
        self.logger.info("Not tracking Switches, switch leaved.")


app_manager.require_app("ryu.topology.switches", api_style=True)
