import logging
import os
from nhd.NHDCommon import NHDCommon
from colorlog import ColoredFormatter
from nhd.CfgTopology import SMTSetting
from nhd.CfgTopology import GpuType
from nhd.CfgTopology import NICCoreDirection
from nhd.CfgTopology import CfgTopology
from pprint import pprint
from typing import Dict, List, Tuple
from itertools import chain

NIC_BW_AVAIL_PERCENT                = 0.9 # Only allow NICs to be scheduled up to this much of their total capacity
SCHEDULABLE_NIC_SPEED_THRESH_MBPS   = 11000 # Don't include NICs for scheduling that are below this speed
ENABLE_SRIOV                        = False # Allow SR-IOV sharing
ENABLE_SHARING                      = False # Allow pods to share a NIC

"""
Properties of a core inside of a node
"""
class NodeCore:
    def __init__(self, core, socket, sib):
        self.core: int = core
        self.sibling: int = sib
        self.socket: int = socket
        self.used: bool = False

    def SetSibling(self, sib):
        self.sibling = sib

"""
Properties of a NIC inside of a node
"""
class NodeNic:
    def __init__(self, ifname: str, mac: str, vendor: str, speed: int, numa_node: int, vfs: 0):
        self.ifname = ifname
        self.vendor = vendor
        self.speed = speed
        self.numa_node = numa_node
        self.speed_used = [0,0] # (rx, tx) set by scheduler
        self.pods_used = 0
        self.num_vfs = vfs

        # The MAC is in a weird format from NFD, so fix it here
        self.mac    = self.FormatMac(mac)

    def SetNodeIndex(self, idx):
        self.idx = idx

    def FormatMac(self, mac):
        return ':'.join(a+b for a,b in zip(mac[::2],mac[1::2])).upper()


"""
Properties of memory inside of a node
"""
class NodeMemory:
    def __init__(self):
        self.ttl_hugepages_gb = 0
        self.free_hugepages_gb = 0


"""
Properties of a GPU inside of a node
"""
class NodeGpu:
    def __init__(self, gtype: str, device_id: int, numa_node: int):
        self.gtype = self.GetType(gtype)
        self.device_id = device_id
        self.numa_node = numa_node
        self.used = False

    def GetType(self, gtype: str):
        if '1080Ti' in gtype:
            return GpuType.GPU_TYPE_GTX_1080TI
        if '1080' in gtype:
            return GpuType.GPU_TYPE_GTX_1080
        if '2080Ti' in gtype:
            return GpuType.GPU_TYPE_GTX_2080TI
        if '2080' in gtype:
            return GpuType.GPU_TYPE_GTX_2080
        if 'V100' in gtype:
            return GpuType.GPU_TYPE_V100

        return GpuType.GPU_TYPE_NOT_SUPPORTED

"""
The Node class holds properties about a node's resources, as well as which resources have been used.
Current resource types in a node are CPUs, GPU, and NICs.
"""
class Node:
    def __init__(self, name):
        self.logger = NHDCommon.GetLogger(__name__)

        self.name = name
        self.cores: List[NodeCore] = []
        self.gpus = []
        self.nics = []

        self.sockets = 0
        self.numa_nodes = 0 
        self.smt_enabled = False
        self.cores_per_proc = 0
        self.pods_scheduled = set()
        self.sriov_en = False
        self.data_vlan = 0
        self.gwip : str = '0.0.0.0/32'
        self.mem: NodeMemory = NodeMemory()
        self.reserved_cores = [] # Reserved CPU cores

    def ResetResources(self):
        """ Resets all resources back to initial values """
        self.logger.info(f'Node {self.name} resetting resources')

        for c in self.cores:
            if c.core not in self.reserved_cores:
                c.used = False

        for g in self.gpus:
            g.used = False

        for n in self.nics:
            n.pods_used = 0
            n.speed_used = [0,0]

        self.mem.free_hugepages_gb = self.mem.ttl_hugepages_gb

        self.pods_scheduled.clear()

    def GetTotalHugepages(self):
        """ Gets the total hugepages for a node """
        return self.mem.ttl_hugepages_gb

    def GetFreeHugepages(self):
        """ Gets the free hugepages for a node """
        return self.mem.free_hugepages_gb        

    def GetNIC(self, mac):
        """ Gets a NIC by MAC address """
        for n in self.nics:
            if n.mac == mac:
                return n
        return None

    def GetNICUsedSpeeds(self):
        """ Gets the RX and TX speeds used on each NIC """
        nicres = []
        for n in self.nics:
            nicres.append(n.speed_used)

        return nicres

    def GetNICFromIfName(self, ifname):
        """ Gets a NIC object from the interface name """
        for n in self.nics:
            if n.ifname == ifname:
                return n
        return None

    def GetTotalPods(self):
        """ Gets the total pods scheduled on the node """
        return len(self.pods_scheduled)

    def PodPresent(self, pod, ns):
        """ Finds if a pod is present on the node """
        return (pod, ns) in self.pods_scheduled

    def AddScheduledPod(self, pod, ns):
        """ Add a scheduled pod to the node """
        self.pods_scheduled.add((pod,ns))
    
    def RemoveScheduledPod(self, pod, ns):
        """ Remove a scheduled pod from the node """
        self.pods_scheduled.remove((pod,ns))

    def GetGPU(self, di):
        """ Gets a GPU by device ID """
        for g in self.gpus:
            if g.device_id == di:
                return g
        return None

    def SMTEnabled(self) -> bool:
        """ Determine if SMT is enabled for this node """
        return self.smt_enabled

    def GetFreeCpuCoreCount(self) -> int:
        """ Gets the number of free CPU cores available. If SMT is enabled we only count cores where both
            siblings are unused.
        """
        if self.smt_enabled:
            return len([x for x in self.cores if not x.used and not self.cores[x.sibling].used])
        else:        
            return len([x for x in self.cores if not x.used])

    def GetFreeGpuCount(self) -> int:
        """ Gets the number of free GPUs """
        return len([x for x in self.gpus if not x.used])

    def GetTotalGPUs(self) -> int:
        """ Gets the total number of GPUs in the node """
        return len(self.gpus)

    def GetTotalCPUs(self) -> int:
        """ Gets the total number of CPUs on the node """
        return len(self.cores)

    def GetFreeCpuCores(self) -> int:
        """ Returns a list containing a list for each socket specifying how many cores + siblings are free. Note that
            we do not allow any multi-tenancy on cores which are already partially used (SMT on and 1/2 logical cores
            are used). """
        self.logger.info(f'Searching for free CPU cores on node {self.name}')

        fl = [0] * self.numa_nodes
        for c in range(self.cores_per_proc*self.sockets):
            if not self.cores[c].used:
                if not self.smt_enabled:
                    fl[self.cores[c].socket] += 1 # Fix in future if we use AMD or other weirdos
                elif not self.cores[self.cores[c].sibling].used:
                    fl[self.cores[c].socket] += 1
    
        return fl

    def GetFreeNumaNicResources(self) -> List[int]:
        """ Return the amount of free NIC resources per NUMA node """
        ninfo = [[] for _ in range(self.numa_nodes)]

        for n in self.nics:
            if ENABLE_SRIOV and n.pods_used == n.num_vfs:
                # If we've used the maximum VFs for this NIC, don't allow any more scheduling
                ninfo[n.numa_node].append([0,0])
            else:
                if ENABLE_SHARING:
                    ninfo[n.numa_node].append([n.speed*NIC_BW_AVAIL_PERCENT - n.speed_used[x] for x in range(2)])
                else:
                    ninfo[n.numa_node].append([0 if (n.pods_used > 0) else n.speed*NIC_BW_AVAIL_PERCENT for x in range(2)])

        return ninfo


    @staticmethod
    def ParseRangeList(rl: str):
        """ Parses a list of numbers separated by commas and hyphens. This is the typical cpuset format
            used by Linux in many boot parameters.
        """
        def ParseRange(r):
            parts = r.split("-")
            return range(int(parts[0]), int(parts[-1])+1)
        return sorted(set(chain.from_iterable(map(ParseRange, rl.split(",")))))         

    def InitCores(self, labels):
        """ Initialize the CPU resouces based on the node labels """
        if not ('feature.node.kubernetes.io/nfd-extras-cpu.num_cores' in labels and 'feature.node.kubernetes.io/nfd-extras-cpu.num_sockets' in labels):
            self.logger.error('Couldn\'t find node CPU labels. Ignoring node')
            return False

        self.sockets        = int(labels['feature.node.kubernetes.io/nfd-extras-cpu.num_sockets'])
        cores               = int(labels['feature.node.kubernetes.io/nfd-extras-cpu.num_cores'])
        self.smt_enabled    = 'feature.node.kubernetes.io/cpu-hardware_multithreading' in labels
        self.numa_nodes     = self.sockets # Fix this if we move to something other than Intel with more NUMA nodes than sockets (AMD)

        self.cores_per_proc = cores // self.sockets

        self.logger.info(f'Initializing CPUs for node {self.name} with procs={self.sockets}, cores={cores}, smt={self.smt_enabled}')
        self.cores = [None]*cores if not self.smt_enabled else [None]*cores*2

        for c in range(len(self.cores)):
            proc = int(int(c % cores) // (cores/self.sockets))
            if self.smt_enabled:
                sib = (c + cores) if c < cores else (c - cores)
            else:
                sib = -1

            self.cores[c] = NodeCore(c, proc, sib)

        if 'feature.node.kubernetes.io/nfd-extras-cpu.isolcpus' not in labels:
            self.logger.info(f'No isolated CPU information found for node {self.name}')
        else:
            # Underscores split the range
            isolrange = labels['feature.node.kubernetes.io/nfd-extras-cpu.isolcpus'].split('_')
            isolcores = []
            for r in isolrange:
                isolcores.extend(Node.ParseRangeList(r))
            self.logger.info(f'Isolated cores in node {self.name} read as {isolrange}')
            ttlcores   = list(range(0, len(self.cores)))

            nonisol = list(set(ttlcores) - set(isolcores))

            # Mark all OS reserved cores as in use
            self.logger.info(f'Marking cores {nonisol} as used')
            for c in range(len(self.cores)):
                if c in nonisol:
                    self.cores[c].used = True
                    self.reserved_cores.append(c)


        self.logger.info(f'Finished setting up cores for node {self.name}')
        return True

    def InitNics(self, labels):
        self.logger.info(f'Initializing NICs for node {self.name}')
        # First check if SR-IOV is enabled. If so, we do not schedule this node using MAC addresses:

        # Fix SR-IOV support with new device plugin
        # for l,v in labels.items():
        #     if ENABLE_SRIOV and ('feature.node.kubernetes.io/nfd-extras-sriov' in l):
        #         self.sriov_en = True
        #         p = l.split('.')
        #         (speed, ifname, vfs) = (p[4], p[5], int(p[6]))

        #         # Skip redundant interface for now...
        #         if 'f1' in ifname:
        #             continue                

        #         if 'Mbs' in speed:
        #             speed = int(speed[:speed.index('Mbs')])
        #         else:
        #             self.logger.info(f'Not adding NIC {ifname} since speed is 0. Interface may be down')
        #             continue

        #         if speed < SCHEDULABLE_NIC_SPEED_THRESH_MBPS:
        #             self.logger.info(f'NIC {ifname} has speed lower than required ({speed} found, '
        #                             f'{SCHEDULABLE_NIC_SPEED_THRESH_MBPS} required. Excluding from schedulable list')
        #             continue

        #         self.nics.append(NodeNic(ifname, 'SR-IOV', 'None', speed/1e3, -1, vfs))
        #         self.logger.info(f'Added SR-IOV NIC with name={ifname}, speed={speed}Mbps, VFs={vfs} to node {self.name}')

        for l,v in labels.items():
            if 'feature.node.kubernetes.io/nfd-extras-nic' in l:
                p = l.split('.')

                (ifname, vendor, mac, speed, numa_node) = (p[4], p[5], p[6], p[7], int(p[8]))

                # Skip redundant interface for now...
                if 'f1' in ifname:
                    continue

                if 'Mbs' in speed:
                    speed = int(speed[:speed.index('Mbs')])
                else:
                    self.logger.info(f'Not adding NIC {ifname} since speed is 0. Interface may be down')
                    continue

                if speed < SCHEDULABLE_NIC_SPEED_THRESH_MBPS:
                    self.logger.info(f'NIC {ifname} has speed lower than required ({speed} found, '
                                    f'{SCHEDULABLE_NIC_SPEED_THRESH_MBPS} required. Excluding from schedulable list')
                    continue

                # If we detected this system is using SR-IOV, only update the existing entry
                if self.sriov_en:
                    nic = self.GetNICFromIfName(ifname)
                    if nic == None:
                        self.logger.error(f'Found NIC {ifname} with SR-IOV enabled on node, but NIC doesn\'t appear to have it enabled. Skipping NIC...')
                        continue
                    
                    nic.numa_node = numa_node
                    nic.mac = nic.FormatMac(mac)

                    self.logger.info(f'Updated SR-IOV NIC with name={ifname}, vendor={vendor}, mac={mac}, speed={speed}Mbps, numa_node={numa_node} to node {self.name}')

                else:
                    self.nics.append(NodeNic(ifname, mac, vendor, speed/1e3, numa_node, 0))
                    self.logger.info(f'Added NIC with name={ifname}, vendor={vendor}, mac={mac}, speed={speed}Mbps, numa_node={numa_node} to node {self.name}')

        # Set all the node indices
        if len(self.nics):
            nidx = [0] * (max([x.numa_node for x in self.nics])+1)
            for n in self.nics:
                self.logger.info(f'Setting NIC node index to {nidx[n.numa_node]} on ifname {n.ifname}')
                n.SetNodeIndex(nidx[n.numa_node])
                nidx[n.numa_node] += 1

        return True

    def InitGpus(self, labels):
        self.logger.info(f'Initializing GPUs for node {self.name}')
        for l,v in labels.items():
            if 'feature.node.kubernetes.io/nfd-extras-gpu' in l:
                p = l.split('.')
                (device_id, gtype, numa_node) = (int(p[4]), p[5], int(p[6]))

                self.gpus.append(NodeGpu(gtype, device_id, numa_node))
                self.logger.info(f'Added GPU with type={gtype}, device_id={device_id}, numa_node={numa_node} to node {self.name}')

        return True

    def InitMisc(self, labels):
        self.logger.info(f'Initializing miscellaneous labels for node {self.name}')
        if 'DATA_PLANE_VLAN' not in labels:
            self.logger.error(f'Couldn\'t find data plane VLAN label for node {self.name}. Skipping node')
            return False
        
        self.data_vlan = int(labels['DATA_PLANE_VLAN'])
        self.logger.info(f'Read data plane VLAN as {self.data_vlan}')

        if 'DATA_DEFAULT_GW' not in labels:
            self.logger.error(f'Couldn\'t find data plane default GW label for node {self.name}. Skipping node')
            return False

        self.gwip = labels['DATA_DEFAULT_GW']
        self.logger.info(f'Read data plane default GW as {self.gwip}')

        return True

    def GetFreeNumaGPUs(self):
        gfree = [0] * self.numa_nodes
        for g in self.gpus:
            if not g.used:
                gfree[g.numa_node] += 1
        
        return gfree

    def SetNodeAddr(self, addr):
        self.logger.info(f'Setting node {self.name} address to {addr}')
        self.addr = addr

    def ParseLabels(self, labels):
        if not self.InitCores(labels):
            return False

        if not self.InitNics(labels):
            return False

        if not self.InitGpus(labels):
            return False

        if not self.InitMisc(labels):
            return False

        return True

    def SetHugepages(self, alloc: int, free: int) -> bool: 
        self.mem.ttl_hugepages_gb  = alloc
        self.mem.free_hugepages_gb = free
        self.logger.info(f'Found {self.mem.free_hugepages_gb}/{self.mem.ttl_hugepages_gb}GB of hugepages allocatable/capacity on node {self.name}')
        return True

    def GetNextGpuFree(self, numa):
        for g in self.gpus:
            if g.numa_node == numa and not g.used:
                return g

        return None                    
     
    def GetFreeCpuBatch(self, numa: int, num: int, smt: SMTSetting) -> List[int]:
        cpus = []
        for ci,c in enumerate(self.cores):
            if num == 0:
                break
            if c.socket == numa and not c.used:
                if self.smt_enabled:
                    if not self.cores[c.sibling].used: # Switch to numa instead of socket later
                        if smt == SMTSetting.SMT_ENABLED and num >= 2:
                            cpus.extend([c.core, c.sibling])
                            num -= 2
                        else:
                            cpus.append(c.core)
                            num -= 1
                else:
                    cpus.append(c.core)
                    num -= 1
        return cpus  

    def PrintResourceStats(self):
        self.logger.info(f'Node {self.name} resource stats:')
        self.logger.info(f'   {self.GetFreeCpuCoreCount()} free CPU cores')
        self.logger.info(f'   {self.GetFreeGpuCount()} free GPU devices')
        self.logger.info(f'   {self.mem.free_hugepages_gb}/{self.mem.ttl_hugepages_gb} hugepages free')
        self.logger.info(f'   NICs:')
        for n in self.nics:
            self.logger.info(f'        {n.mac}: {n.speed_used[0]}/{n.speed_used[1]} Gbps used on {n.speed} Gbps interface with {n.pods_used} pods using interface')
        


    def RemoveResourcesFromTopology(self, top):
        """ Remove resources from a node that are present in a topology structure. """

        for pv in top.proc_groups:
            for m in pv.misc_cores:
                if self.cores[m.core].used:
                    self.logger.error(f'Processing group misc core {m.core} was already in use!')
                self.cores[m.core].used = True

            for m in pv.proc_cores:
                if self.cores[m.core].used:
                    self.logger.error(f'Processing group core {m.core} was already in use!')
                self.cores[m.core].used = True

            for g in pv.group_gpus:
                dev = self.GetGPU(g.device_id)
                if dev is None:
                    self.logger.error(f'Cannot find GPU device ID {g.device_id}')
                else:
                    if dev.used:
                        self.logger.error(f'GPU {dev.device_id} was already in use!')

                    self.logger.info(f'Taking GPU device ID {g.device_id}')
                    dev.used = True

                for c in g.cpu_cores:
                    if self.cores[c.core].used:
                        self.logger.error(f'GPU core {c.core} was already in use!')
                    self.cores[c.core].used = True

        for m in top.misc_cores:
            if self.cores[m.core].used:
                self.logger.error(f'Miscellaneous core {m.core} was already in use!')
            self.cores[m.core].used = True

        for p in top.nic_core_pairing:
            nic = self.GetNIC(p.mac) if not self.sriov_en else self.GetNICFromIfName(p.mac)
            if nic is None:
                self.logger.error(f'Cannot find NIC {p.mac} on node!')
                continue
            
            nic.speed_used[0] += p.rx_core.nic_speed
            nic.speed_used[1] += p.tx_core.nic_speed
            self.logger.info(f'Speeds used on NIC after removing resources = {nic.speed_used[0]}/{nic.speed_used[1]}')

            nic.pods_used += 1

        if top.hugepages_gb > 0:
            self.mem.free_hugepages_gb -= top.hugepages_gb    
            self.logger.info(f'Taking {top.hugepages_gb} 1GB hugepages from node. {self.mem.free_hugepages_gb} remaining')                  


    def AddResourcesFromTopology(self, top):
        """ Add resources from a node that are present in a topology structure. """

        for pv in top.proc_groups:
            for m in pv.misc_cores:
                if not self.cores[m.core].used:
                    self.logger.error(f'Processing misc core {m.core} was not in use!')
                self.cores[m.core].used = False

            for m in pv.proc_cores:
                if not self.cores[m.core].used:
                    self.logger.error(f'Processing core {m.core} was not in use!')
                self.cores[m.core].used = False

            for g in pv.group_gpus:
                dev = self.GetGPU(g.device_id)
                if dev is None:
                    self.logger.error(f'Cannot find GPU device ID {g.device_id}')
                else:
                    if not dev.used:
                        self.logger.error(f'GPU {dev.device_id} was not in use!')

                    dev.used = False

                for c in g.cpu_cores:
                    if not self.cores[c.core].used:
                        self.logger.error(f'GPU core {c.core} was not in use!')
                    self.cores[c.core].used = False

        for m in top.misc_cores:
            if not self.cores[m.core].used:
                self.logger.error(f'Misc core {m.core} was not in use!')
            self.cores[m.core].used = False

        for p in top.nic_core_pairing:
            nic = self.GetNIC(p.mac) if not self.sriov_en else self.GetNICFromIfName(p.mac)
            if nic is None:
                self.logger.error(f'Cannot find NIC {p.mac} on node!')
                continue
            
            nic.speed_used[0] -= p.rx_core.nic_speed
            nic.speed_used[1] -= p.tx_core.nic_speed
            self.logger.info(f'Speeds used on NIC after adding resources = {nic.speed_used[0]}/{nic.speed_used[1]}')

            nic.pods_used -= 1

        # Hugepages requests
        if top.hugepages_gb > 0:
            self.mem.free_hugepages_gb += top.hugepages_gb    
            self.logger.info(f'Adding {top.hugepages_gb} 1GB hugepages to node. {self.mem.free_hugepages_gb} remaining')                    
    
    def GetNADListFromIndices(self, ilist: List[int]):
        """ Get the NAD list from the NIC indices """
        names = [self.nics[i].ifname for i in ilist]
        self.logger.info(f'Built NetworkAttachmentDefinition of {names}')
        return names


    def ClaimPodNICResources(self, nidx):
        for ni in nidx: # Mark as pod using the interface
            self.nics[ni].pods_used += 1

    def SetPhysicalIdsFromMapping(self, mapping, top: CfgTopology):
        """ Maps the indices after the mapping function is done into physical node resources based on what's free. Uses
            the previously-defined topology to pull the actual groups out """
        
        # Set up the "used" resources.
        used_cpus = []
        used_gpus = []
        used_nics = []
        
        try:
            # Go through each of the processing groups and map resources
            for pi,pv in enumerate(top.proc_groups):
                # Set VLAN
                self.logger.info(f'Setting VLAN to {self.data_vlan}')
                pv.vlan.vlan = self.data_vlan

                group_numa_node = mapping['gpu'][pi]
                cidx = 0 # Keep track of which CPU cores we've given out
                gcpu_req = len(pv.proc_cores) + sum([len(gpu.cpu_cores) for gpu in pv.group_gpus])
                group_cpus = self.GetFreeCpuBatch(group_numa_node, gcpu_req, pv.proc_smt)
                self.logger.info(f'Got {group_cpus} processing group cores for group {pi}')

                if len(group_cpus) != gcpu_req:
                    self.logger.error(f'Asked for {gcpu_req} free CPUs, but only got {len(group_cpus)} back!')
                    raise IndexError

                # Assign GPU device IDs and CPU cores
                for gv in pv.group_gpus:
                    gdev = self.GetNextGpuFree(group_numa_node)
                    if gdev == None:
                        self.logger.error(f'No free GPUs available on node {self.name} even though mapping found one!')
                        raise IndexError

                    self.logger.info(f'Got GPU with device ID {gdev.device_id}')                        
                    
                    gv.device_id = gdev.device_id
                    gdev.used = True
                    used_gpus.append(gdev.device_id)

                    for gpu_cpu in gv.cpu_cores:
                        gpu_cpu.core = group_cpus[cidx]
                        self.cores[gpu_cpu.core].used = True
                        cidx += 1

                # Assign processing cores
                for groupc in pv.proc_cores:
                    groupc.core = group_cpus[cidx]
                    self.cores[groupc.core].used = True
                    cidx += 1

                    # Check if this core is using NIC resources
                    if groupc.nic_dir in (NICCoreDirection.NIC_CORE_DIRECTION_RX, NICCoreDirection.NIC_CORE_DIRECTION_TX):
                        nicmap = mapping['nic'][pi][1]
                        idx = -1
                        for ni, nic in enumerate(self.nics):
                            if nicmap == nic.idx and nic.numa_node == group_numa_node:
                                idx = ni
                                break

                        if idx == -1:
                            self.logger.error(f'Couldn\'t find NIC index for NUMA node {group_numa_node} on node')
                            raise IndexError
                        else:
                            sidx = 0 if groupc.nic_dir == NICCoreDirection.NIC_CORE_DIRECTION_RX else 1
                            self.nics[idx].speed_used[sidx] += groupc.nic_speed
                            used_nics.append((idx, groupc.nic_speed, groupc.nic_dir))

                        # Set physical NIC resources
                        ng = top.GetNICGroup(groupc)
                        if ng is None:
                            self.logger.error(f'Couldn\'t find core {groupc.name} in nic group list!')
                            raise IndexError
                        
                        self.logger.info(f'Adding interface {self.nics[idx].ifname} with mac {self.nics[idx].mac} to core {groupc.core}')
                        if self.sriov_en: # SR-IOV maps by interface name instead of MAC
                            ng.AddInterface(self.nics[idx].ifname)
                        else:
                            ng.AddInterface(self.nics[idx].mac)

                # Check that we used all the CPU cores
                if cidx != len(group_cpus):
                    self.logger.info('Still have {len(group_cpus) - cidx} leftover CPUs in request list!')
                    raise IndexError
                
                used_cpus.extend(group_cpus)


                # Assign the helper cores
                helper_req = self.GetFreeCpuBatch(group_numa_node, len(pv.misc_cores), pv.helper_smt)
                self.logger.info(f'Got {helper_req} helper cores for group {pi}')
                cidx = 0
                if len(pv.misc_cores) != len(helper_req):
                    self.logger.error(f'Asked for {len(pv.misc_cores)} free helper CPUs, but only got {len(helper_req)} back!')
                    return None

                for hc in pv.misc_cores:
                    hc.core = helper_req[cidx]
                    self.cores[hc.core].used = True
                    cidx += 1

                if cidx != len(helper_req):
                    self.logger.info('Still have {len(helper_req) - cidx} leftover helper CPUs in request list!')
                    return None
                    
                used_cpus.extend(helper_req)

            # Set data plane default GWs
            top.SetDataDefaultGw(self.gwip)

            # Hugepages requests
            if top.hugepages_gb > 0:
                self.mem.free_hugepages_gb -= top.hugepages_gb    
                self.logger.info(f'Taking {top.hugepages_gb} 1GB hugepages from node. {self.mem.free_hugepages_gb} remaining')                    

            # Last, we assign the top-level miscellaneous cores. Miscellaneous cores are the last element in the CPU list
            misc_cpus = self.GetFreeCpuBatch(mapping['cpu'][-1], len(top.misc_cores), top.misc_cores_smt)
            self.logger.info(f'Got {misc_cpus} top-level miscellaneous cores')

            cidx = 0
            if len(top.misc_cores) != len(misc_cpus):
                self.logger.error(f'Asked for {top.misc_cores} free helper CPUs, but only got {len(misc_cpus)} back!')
                return None

            for mc in top.misc_cores:
                mc.core = misc_cpus[cidx]
                self.cores[mc.core].used = True
                cidx += 1

            if cidx != len(misc_cpus):
                self.logger.info('Still have {len(misc_cpus) - cidx} leftover helper CPUs in request list!')
                return None

            used_cpus.extend(misc_cpus)

            self.logger.info(f'Setting control VLAN to {self.data_vlan}')
            top.ctrl_vlan.vlan = self.data_vlan

            self.logger.info('All assignments completed successfully')
            self.logger.info(f'CPU assignments: {used_cpus}')
            self.logger.info(f'GPU assignments: {used_gpus}')
            self.logger.info(f'NIC assignments: {used_nics}')

        except IndexError:
            self.logger.info('One or more failures assigning resources to node. Unwinding mapping and returning...')
            for c in used_cpus:
                self.cores[c].used = False
            for g in used_gpus:
                self.gpus[g].used = False
            for n in used_nics:
                if n[2] == NICCoreDirection.NIC_CORE_DIRECTION_RX:
                    self.nics[n[0]].speed_used[0] -= self.nics[n[1]]
                else:
                    self.nics[n[0]].speed_used[1] -= self.nics[n[1]]
            
            raise
        
        self.logger.info(f'Node {self.name} has {self.GetFreeCpuCoreCount()} CPU cores and {self.GetFreeGpuCount()} free GPUs left')

        return used_nics # The NIC list is used to populate the network attachment definitions externally

        
