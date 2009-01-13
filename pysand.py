#!/usr/bin/env python

import os, pwd, sys
import nids
from ident_gen import *

end_states = (nids.NIDS_CLOSE, nids.NIDS_TIMEOUT, nids.NIDS_RESET)
NOTROOT = "george"
DEBUG=False


class certainty_node: # One per protocol per stream
    """A certainty node describes a single node in the certainty table;
    there should exist a single certainty node per protocol per stream.
    It handles tracking our certainty about its identification."""
    def __init__(self, identifier):
        """Construct a new certainty node based on an identifier object."""
        self.ident=identifier
        self.next={'c': 0, 's': 0}
        self.curs={'c': 0, 's': 0}
        self.certainty=0
    
    def next_search(self,half_stream='c'):
        """Returns the next string to search the half-stream for, depending
        upon the character that is passed as the half_stream parameter.
        Returns None if there are no more signatures to find in the half-stream.
        c->client half-stream (default)
        s->server half-stream"""
        
        # Enforce default behavior
        if half_stream not in ('c','s'):
            half_stream='c'
            if DEBUG: print "Forcing client search. Fix your coding, stupid."
        
        # Select the proper set of signatures
        if half_stream=='c':
            sigs=self.ident.c_sigs
        else:
            sigs=self.ident.s_sigs
        
        # Return the proper signature, if it exists. Otherwise None.
        if len(sigs) <= self.next[half_stream]:
            return None
        else:
            return sigs[self.next[half_stream]]

class sand:
    """The main pysand class. Instanciating more than one at a time
    is not recommended."""
    def __init__(self, detect_callback_tcp, id_callback_tcp, end_callback_tcp, identifier_dir, pcap_file=None):
        """Construct a new pysand object.
        Parameters:
        detect_callback_tcp: Callback function to be called when a new stream is detected.
        id_callback_tcp: Callback function to be called when a stream is identified.
        end_callback_tcp: Callback function to be called when a stream is closed.
        identifier_dir: The directory containing our identifier specifications.
        pcap_file: Optional. The pcap file to use. If omitted, captures from the wire."""
        
        if pcap_file == 'None':
            pcap_file=None
        
        # Load all the identifiers from the specified directory.
        self.all_idents=self.load_idents(identifier_dir)
        
        # Storage init
        self.next_index=0
        self.stream_table=dict()
        self.index_table=dict()
        
        # Set up libnids
        nids.param("scan_num_hosts", 0)  # Disable portscan detection        
        if pcap_file is not None:
            nids.param("filename", pcap_file)
        nids.param("pcap_filter", "tcp") # Only capture TCP traffic
        nids.init()
        nids.register_tcp(self.handleTcpStream) # Maybe put after the next line. We'll see.
        
        # SAND Callback functions init
        self.f_cb_new_tcp = detect_callback_tcp
        self.f_cb_id_tcp = id_callback_tcp
        self.f_cb_end_tcp = end_callback_tcp
        if DEBUG: print "Callbacks"

        # Set up so as not to run as root any longer? You need to be root to
        # invoke this, though. I don't think I like this malarkey. Real men run as root.
        (uid, gid) = pwd.getpwnam(NOTROOT)[2:4]
        os.setgroups([gid,])
        os.setgid(gid)
        os.setuid(uid)
        if 0 in [os.getuid(), os.getgid()] + list(os.getgroups()):
            print "error - drop root, please!"
            sys.exit(1)
        
        # Output our PID. Just in case we have to kill us.
        print "pid", os.getpid()
    
        # Loop forever (network device), or until EOF (pcap file)
        # Note that an exception in the callback will break the loop!
        try:
            nids.run()
        except nids.error, e:
            print "nids/pcap error:", e
        except Exception, e:
            print "misc. exception (runtime error in user callback?):", e
        
        # When finished, print debugging information:
        if DEBUG:
            for index,strm in self.index_table.iteritems():
                    print "State of stream", strm[2], ",", str(strm[0].addr), ":", strm[0].nids_state,":",strm[3]

    def load_idents(self, ident_dir):
        """Load into memory all of the protocol identifiers from ident_dir."""
        identifiers=[]
        for file in os.listdir(ident_dir): # For every file in ident_dir
            new_identifier = load_ident(os.path.join(ident_dir,file))
            if new_identifier is not None:
                identifiers+=[new_identifier]
                print "Added",os.path.join(ident_dir,file)
        return identifiers

    def handleTcpStream(self, tcp_stream):
        """Callback function called by libnids when it receives a new packet."""
        if DEBUG: print "*****tcps -", str(tcp_stream.addr), " state:", tcp_stream.nids_state, " - ", tcp_stream
        stream_id=tcp_stream.addr
        if tcp_stream.nids_state == nids.NIDS_JUST_EST: # New connection/stream
            #self.stream_list = self.stream_list+[tcp_stream]
            tcp_stream.client.collect=1 # Signal to collect this data
            tcp_stream.server.collect=1
            
            # Store our own metadata -- twice.
            new_ct=self.certainty_table(self.all_idents)
            self.index_table[self.next_index]=(tcp_stream,new_ct,stream_id,'unknown')
            self.stream_table[stream_id]=(self.next_index,new_ct,tcp_stream,'unknown')
            self.next_index+=1
            
            self.identifyStream(stream_id)
            self.f_cb_new_tcp(tcp_stream) #Call back.
        elif tcp_stream.nids_state == nids.NIDS_DATA: # Established connection receiving new data
            tcp_stream.discard(0) # Don't discard any of it. Keep following.
            index=self.stream_table[stream_id][0]
            ct = self.index_table[index][1]
            proto=self.index_table[index][3]
            if tcp_stream != self.index_table[index][0]:
                self.index_table[index]=(tcp_stream,ct,stream_id,proto)
                self.stream_table[stream_id]=(index,ct,tcp_stream,proto)
            if proto is 'unknown':
                self.identifyStream(stream_id)
        elif tcp_stream.nids_state in end_states: #TODO: This doesn't seem to work.
            self.f_cb_end_tcp(tcp_stream)

    def identifyStream(self, stream_id):
        id_ret=self.searchStream(stream_id)
        if id_ret:
            index=self.stream_table[stream_id][0]
            tcp_stream=self.stream_table[stream_id][2]
            ct=self.stream_table[stream_id][1]
            self.stream_table[stream_id]=(index,ct,tcp_stream,id_ret)
            self.index_table[index]=(tcp_stream,ct,stream_id,id_ret)
            self.f_cb_id_tcp(self.stream_table[stream_id][2], id_ret)
        pass
            
    def searchStream(self,stream_id):
        tcp_stream=self.stream_table[stream_id][2]
        data = dict()
        data['c'] = tcp_stream.server.data # These needed to be switched.
        data['s'] = tcp_stream.client.data # I don't think it's my failt.
        for cert_index in self.stream_table[stream_id][1]:
            cert_node=self.stream_table[stream_id][1][cert_index]
            for half_stream in ('s','c'):
                search_term=None
                while search_term is not cert_node.next_search(half_stream):
                    search_term=cert_node.next_search(half_stream)
                    if search_term is not None: # None => no more client sigs to find.
                        found_loc = str(data[half_stream]).find(cert_node.next_search(half_stream)[0]) #, cert_node.curs[half_stream])
                        if DEBUG: print "Searching for",cert_node.next_search(half_stream)[0], "in",half_stream,"in",stream_id
                        if found_loc is not -1:
                            cert_node.certainty+=1
                            if DEBUG: print "I found",cert_node.next_search(half_stream)[0],"-- Certainty of ID is ", cert_node.certainty, " / ", cert_node.ident.threshold
                            #print cert_node.next_search(half_stream)[0], "in\n",str(data[half_stream])
                            cert_node.next[half_stream]+=1
                            if cert_node.certainty == cert_node.ident.threshold:
                                tcp_stream.client.collect = 0
                                tcp_stream.server.collect = 0
                                return cert_node.ident.proto_name
                        else:
                            pass
                    else:
                        if DEBUG: print "No more sigs to find."
        return False
                    
    
    def certainty_table(self,identifiers):
        ct=dict()
        for i in identifiers:
            ct[i.proto_name]=certainty_node(i)
        return ct

def main():
    libsand = sand(newStream,idStream,endStream,sys.argv[2],sys.argv[1])
    print "done"
    pass

def newStream(tcp_stream):
    pass
    #print "New stream opened: ", tcp_stream.addr

def idStream(tcp_stream, proto_name):
    pass
    #print "Identification made:", tcp_stream.addr, "is", proto_name
    #if(139 in tcp_stream.addr[1]):
    #    print str(tcp_stream.client.data)

def endStream(tcp_stream):
    pass
    #print "Stream closed: ", tcp_stream.addr

if __name__ == '__main__':
    DEBUG=False
    if len(sys.argv) == 3: # read a pcap file?
        pass
    else:
        print "Oops, wrong input! Put: sudo python pysand.py pcapfile identdir"
        sys.exit()
    main()
