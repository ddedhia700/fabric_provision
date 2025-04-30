import argparse
import csv
import ipaddress
import os
import re
import yaml
from typing import Dict, List, Any, Optional
from jinja2 import Environment, FileSystemLoader
from itertools import islice

# Config Reader Functions
def read_config(config_path: str, input_path: Optional[str] = None, user_inputs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read and merge fabric.yml, input.yml, and user inputs."""
    config = {}
    
    # Read input.yml to get fabric_context if provided
    input_config = {}
    fabric_context = None
    if input_path:
        input_config = _read_yaml(input_path, "input configuration")
        fabric_context = input_config.get("fabric_context")
    
    # Apply user inputs if provided
    if user_inputs:
        input_config.update(user_inputs)
        fabric_context = input_config.get("fabric_context", fabric_context)
    
    # Read fabric.yml and extract config for fabric_context
    fabric_config_raw = _read_yaml(config_path, "fabric configuration")
    if not fabric_context:
        raise ValueError("fabric_context must be specified in input.yml or user inputs")
    
    if fabric_context not in fabric_config_raw:
        raise ValueError(f"fabric_context '{fabric_context}' not found in fabric.yml")
    
    fabric_config = fabric_config_raw[fabric_context]
    if not isinstance(fabric_config, dict):
        raise ValueError(f"fabric_context '{fabric_context}' in fabric.yml must be a dictionary")
    
    config.update(fabric_config)
    config.update(input_config)
    
    # Store fabric_context in config for later use
    config["fabric_context"] = fabric_context
    
    _validate_config(config)
    return config

def _read_yaml(file_path: str, file_type: str) -> Dict[str, Any]:
    """Read YAML file."""
    try:
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        if not data:
            return {}
        return data
    except FileNotFoundError:
        raise FileNotFoundError(f"{file_type.capitalize()} file not found: {file_path}")
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML format in {file_type} file: {e}")

def _validate_config(config: Dict[str, Any]):
    """Validate configuration settings for present keys."""
    # Validate network addresses if provided
    transit_net = None
    loopback_net = None
    if "transit" in config:
        try:
            transit_net = ipaddress.ip_network(config["transit"], strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid transit network address: {e}")
    if "loopback" in config:
        try:
            loopback_net = ipaddress.ip_network(config["loopback"], strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid loopback network address: {e}")
    
    # Check for network overlap
    if transit_net and loopback_net and transit_net.overlaps(loopback_net):
        raise ValueError("Transit and loopback networks overlap")

    # Validate numeric inputs if provided
    if "num_of_spines" in config and (not isinstance(config["num_of_spines"], int) or config["num_of_spines"] < 1):
        raise ValueError("num_of_spines must be a positive integer")
    if "num_of_leafs" in config:
        if not isinstance(config["num_of_leafs"], int) or config["num_of_leafs"] < 1:
            raise ValueError("num_of_leafs must be a positive integer")
        # Require even number of leafs only if leaf_pair is true
        if config.get("leaf_pair") in [True, "yes", "true"] and config["num_of_leafs"] % 2 != 0:
            raise ValueError("num_of_leafs must be even when leaf_pair is enabled")
    if "bgp_asn" in config and (not isinstance(config["bgp_asn"], int) or config["bgp_asn"] < 1):
        raise ValueError("bgp_asn must be a positive integer")

    # Validate IP address availability
    if "num_of_spines" in config and "num_of_leafs" in config and loopback_net:
        total_devices = config["num_of_spines"] + config["num_of_leafs"]
        # Calculate usable IPs (exclude network and broadcast for non-/31 or /32)
        usable_ips = sum(1 for _ in loopback_net.hosts()) if loopback_net.prefixlen < 31 else loopback_net.num_addresses
        if usable_ips < total_devices:
            raise ValueError(f"Loopback network {loopback_net} has only {usable_ips} usable IPs, need {total_devices} for {total_devices} devices")
    
    if "num_of_leafs" in config and transit_net:
        required_subnets = config["num_of_leafs"]
        if config.get("leaf_pair") in [True, "yes", "true"]:
            required_subnets += config["num_of_leafs"] // 2
        # Calculate maximum /31 subnets possible
        max_subnets = transit_net.num_addresses // 2  # Each /31 uses 2 addresses
        if max_subnets < required_subnets:
            raise ValueError(f"Transit network {transit_net} can provide {max_subnets} /31 subnets, need {required_subnets}")

    # Parse and validate leaf_spine_ports if provided
    if "leaf_spine_ports" in config:
        try:
            config["leaf_spine_ports"] = parse_flexible_range(config["leaf_spine_ports"])
            if not config["leaf_spine_ports"]:
                raise ValueError("leaf_spine_ports must not be empty")
            if len(set(config["leaf_spine_ports"])) != len(config["leaf_spine_ports"]):
                raise ValueError("Duplicate port numbers in leaf_spine_ports")
        except ValueError as e:
            raise ValueError(f"Invalid leaf_spine_ports: {e}")

    # Parse and validate inter_leaf_ports if provided
    if "inter_leaf_ports" in config:
        try:
            config["inter_leaf_ports"] = parse_flexible_range(config["inter_leaf_ports"])
            if not config["inter_leaf_ports"]:
                raise ValueError("inter_leaf_ports must not be empty")
            if len(set(config["inter_leaf_ports"])) != len(config["inter_leaf_ports"]):
                raise ValueError("Duplicate port numbers in inter_leaf_ports")
        except ValueError as e:
            raise ValueError(f"Invalid inter_leaf_ports: {e}")

    # Parse and validate spine port ranges if provided
    if "spine_port_channel_range" in config:
        try:
            config["spine_port_channel_range"] = parse_flexible_range(config["spine_port_channel_range"])
            if not config["spine_port_channel_range"]:
                raise ValueError("spine_port_channel_range must not be empty")
            if len(set(config["spine_port_channel_range"])) != len(config["spine_port_channel_range"]):
                raise ValueError("Duplicate port numbers in spine_port_channel_range")
        except ValueError as e:
            raise ValueError(f"Invalid spine_port_channel_range: {e}")
    
    if "spine_ports_range" in config:
        try:
            config["spine_ports_range"] = parse_flexible_range(config["spine_ports_range"])
            if not config["spine_ports_range"]:
                raise ValueError("spine_ports_range must not be empty")
            if len(set(config["spine_ports_range"])) != len(config["spine_ports_range"]):
                raise ValueError("Duplicate port numbers in spine_ports_range")
            # Ensure enough ports for leaf connections
            if "num_of_leafs" in config and "leaf_spine_ports" in config:
                if len(config["spine_ports_range"]) < config["num_of_leafs"] * len(config["leaf_spine_ports"]):
                    raise ValueError("spine_ports_range does not have enough ports for all leaf connections")
        except ValueError as e:
            raise ValueError(f"Invalid spine_ports_range: {e}")

# Input Validator Functions
def parse_hostname_range(hostname_input: str) -> List[str]:
    """Parse hostname range (e.g., nj01pamr[101-106], nj01pamr[101a-101c])."""
    match = re.match(r"^([^\[]+)\[([0-9a-zA-Z]+)-([0-9a-zA-Z]+)\]$", hostname_input)
    if not match:
        raise ValueError(f"Invalid hostname range format: {hostname_input}. Expected format: prefix[start-end]")
    
    prefix, start, end = match.groups()
    
    if start.isdigit() and end.isdigit():
        start_num, end_num = int(start), int(end)
        if start_num > end_num:
            raise ValueError(f"Invalid numeric range: start ({start}) must be less than or equal to end ({end})")
        return [f"{prefix}{i:03d}" for i in range(start_num, end_num + 1)]
    
    if len(start) != len(end):
        raise ValueError(f"Start ({start}) and end ({end}) must have same length for alphanumeric range")
    start_base, start_suffix = start[:-1], start[-1]
    end_base, end_suffix = end[:-1], end[-1]
    if start_base != end_base or not start_suffix.isalpha() or not end_suffix.isalpha():
        raise ValueError("Alphanumeric range must have same base and alphabetic suffixes (e.g., 101a-101c)")
    
    start_ord, end_ord = ord(start_suffix.lower()), ord(end_suffix.lower())
    if start_ord > end_ord:
        raise ValueError(f"Invalid alphanumeric range: start ({start_suffix}) must be less than or equal to end ({end_suffix})")
    
    return [f"{prefix}{start_base}{chr(i)}" for i in range(start_ord, end_ord + 1)]

def parse_flexible_range(range_input: Any) -> List[int]:
    """Parse flexible range format (e.g., [1,3,5-10] or '[1,3,5-10]')."""
    try:
        if isinstance(range_input, str):
            range_str = range_input.strip()
        elif isinstance(range_input, list):
            range_str = ",".join(str(item).strip() for item in range_input)
            range_str = f"[{range_str}]"
        else:
            raise ValueError(f"Invalid range input type: {type(range_input)}. Expected string or list")

        if not range_str.startswith("[") or not range_str.endswith("]"):
            raise ValueError(f"Invalid range format: {range_str}. Expected format: [1,3,5-10]")
        
        range_str = range_str[1:-1].strip()
        if not range_str:
            return []
        
        result = set()
        items = range_str.split(",")
        
        for item in items:
            item = item.strip()
            if not item:
                continue
            if "-" in item:
                match = re.match(r"^(\d+)-(\d+)$", item)
                if not match:
                    raise ValueError(f"Invalid range format in item: {item}. Expected: start-end")
                start, end = map(int, match.groups())
                if start < 1 or end < 1:
                    raise ValueError(f"Range values must be positive integers: {item}")
                if start > end:
                    raise ValueError(f"Invalid range: start ({start}) must be less than or equal to end ({end})")
                result.update(range(start, end + 1))
            else:
                try:
                    num = int(item)
                    if num < 1:
                        raise ValueError(f"Port number must be a positive integer: {item}")
                    result.add(num)
                except ValueError as e:
                    raise ValueError(f"Invalid number format in item: {item}. Error: {e}")
        
        return sorted(list(result))
    except Exception as e:
        raise ValueError(f"Failed to parse range {range_input}: {e}")

def validate_input(num_spines: int, num_leafs: int, spines: List[str], leafs: List[str], config: Dict[str, Any]):
    """Validate user inputs."""
    if num_spines < 1:
        raise ValueError("Number of spines must be greater than 0")
    if num_leafs < 1:
        raise ValueError("Number of leafs must be greater than 0")
    if len(spines) != num_spines:
        raise ValueError(f"Expected {num_spines} spine hostnames, got {len(spines)}")
    if len(leafs) != num_leafs:
        raise ValueError(f"Expected {num_leafs} leaf hostnames, got {len(leafs)}")
    if len(set(spines + leafs)) != len(spines + leafs):
        raise ValueError("Duplicate hostnames detected")
    
    hostname_pattern = re.compile(r"^[a-zA-Z0-9-]+$")
    for hostname in spines + leafs:
        if not hostname_pattern.match(hostname):
            raise ValueError(f"Invalid hostname: {hostname}. Only alphanumeric characters and hyphens allowed")

# IP Utilities Functions
def generate_subnets(network: str, prefix_len: str) -> List[ipaddress.IPv4Network]:
    """Generate list of subnets from a network."""
    net = ipaddress.ip_network(network)
    return list(net.subnets(new_prefix=int(prefix_len.lstrip('/'))))

def generate_ips(network: str, start: int, count: int) -> List[str]:
    """Generate list of IPs from a network starting at offset."""
    net = ipaddress.ip_network(network)
    return [str(ip) for ip in list(net)[start:start + count]]

def assign_ips(devices: Dict[str, List[Dict]], num_leafs: int, config: Dict[str, Any]) -> None:
    """Assign IPs without state persistence."""
    # Minimal defaults for IP assignment
    transit_network = config.get("transit", "10.10.10.0/24")
    loopback_network = config.get("loopback", "10.10.20.0/24")
    leaf_spine_linknet = config.get("leaf_spine_linknet", "/31")
    inter_leaf_linknet = config.get("inter_leaf_linknet", "/31")
    
    transit_subnets = generate_subnets(transit_network, leaf_spine_linknet)
    
    # Assign continuous loopback IPs for all devices
    total_devices = len(devices["spines"]) + len(devices["leafs"])
    loopback_ips = generate_ips(loopback_network, 1, total_devices)
    
    ip_index = 0
    for spine in devices["spines"]:
        spine["loopback_ip"] = f"{loopback_ips[ip_index]}/32"
        ip_index += 1
    
    for leaf in devices["leafs"]:
        leaf["loopback_ip"] = f"{loopback_ips[ip_index]}/32"
        ip_index += 1
    
    # Assign leaf-spine transit IPs
    transit_index = 0
    for leaf_idx, leaf in enumerate(devices["leafs"]):
        is_odd_leaf = leaf_idx % 2 == 0
        spine_idx = 0 if is_odd_leaf else 1
        spine = devices["spines"][spine_idx]
        
        transit_net = transit_subnets[transit_index]
        transit_ips = [str(ip) for ip in islice(transit_net, 2)]
        
        leaf_ints = [intf for intf in leaf["interfaces"] if intf["peer"] == spine["hostname"]]
        spine_ints = [intf for intf in spine["interfaces"] if intf["peer"] == leaf["hostname"]]
        for leaf_int in leaf_ints:
            leaf_int.update({"ip": f"{transit_ips[0]}/31", "transit_subnet": str(transit_net)})
        for spine_int in spine_ints:
            spine_int.update({"ip": f"{transit_ips[1]}/31", "transit_subnet": str(transit_net)})
        
        leaf["bgp_neighbors"] = [
            {"hostname": spine["hostname"], "neighbor_ip": spine["loopback_ip"].split("/")[0], "remote_asn": config.get("bgp_asn", 65000)}
            for spine in devices["spines"]
        ]
        transit_index += 1
    
    # Assign inter-leaf IPs if leaf_pair is enabled
    if config.get("leaf_pair") in [True, "yes", "true"]:
        inter_leaf_subnets = list(islice(transit_subnets, num_leafs, num_leafs + (num_leafs // 2)))
        inter_leaf_index = 0
        for pair_idx in range(0, num_leafs, 2):
            leaf1 = devices["leafs"][pair_idx]
            leaf2 = devices["leafs"][pair_idx + 1]
            
            inter_leaf_net = inter_leaf_subnets[inter_leaf_index]
            inter_leaf_ips = [str(ip) for ip in islice(inter_leaf_net, 2)]
            
            leaf1_ints = [intf for intf in leaf1["interfaces"] if intf["peer"] == leaf2["hostname"]]
            leaf2_ints = [intf for intf in leaf2["interfaces"] if intf["peer"] == leaf1["hostname"]]
            for leaf1_int in leaf1_ints:
                leaf1_int.update({"ip": f"{inter_leaf_ips[0]}/31", "transit_subnet": str(inter_leaf_net)})
            for leaf2_int in leaf2_ints:
                leaf2_int.update({"ip": f"{inter_leaf_ips[1]}/31", "transit_subnet": str(inter_leaf_net)})
            inter_leaf_index += 1

# Device Manager Functions
def initialize_devices(num_spines: int, num_leafs: int, spines: List[str], leafs: List[str], 
                      config: Dict[str, Any]) -> Dict[str, List[Dict]]:
    """Initialize spine and leaf devices."""
    devices = {"spines": [], "leafs": []}
    for i, hostname in enumerate(spines[:num_spines]):
        devices["spines"].append({
            "hostname": hostname,
            "role": "spine",
            "interfaces": [],
            "bgp_asn": config.get("bgp_asn", 65000),
            "loopback_ip": None,
            "bgp_neighbors": []
        })
    for i, hostname in enumerate(leafs[:num_leafs]):
        pair_id = (i // 2) + 1 if config.get("leaf_pair") in [True, "yes", "true"] else None
        devices["leafs"].append({
            "hostname": hostname,
            "role": "leaf",
            "interfaces": [],
            "bgp_asn": config.get("bgp_asn", 65000),
            "pair_id": pair_id,
            "loopback_ip": None,
            "bgp_neighbors": []
        })
    return devices

def build_connectivity(devices: Dict[str, List[Dict]], num_leafs: int, config: Dict[str, Any]):
    """Build leaf-spine and inter-leaf connectivity."""
    # Minimal defaults for connectivity
    leaf_spine_ports = config.get("leaf_spine_ports", [47, 48])
    spine_ports_range = config.get("spine_ports_range", list(range(1, num_leafs * len(leaf_spine_ports) + 1)))
    
    spine_port_index = 0
    for leaf_idx, leaf in enumerate(devices["leafs"]):
        is_odd_leaf = leaf_idx % 2 == 0
        spine_idx = 0 if is_odd_leaf else 1
        spine = devices["spines"][spine_idx]
        local_po = config.get("leaf_spine_port_channel_id", 1)
        remote_po = config.get("leaf_spine_port_channel_id", 1)
        
        for port_idx, leaf_port_num in enumerate(leaf_spine_ports):
            leaf_port = f"Ethernet{leaf_port_num}"
            spine_port_num = spine_ports_range[spine_port_index + port_idx]
            spine_port = f"Ethernet{spine_port_num}"
            leaf["interfaces"].append({
                "name": f"Port-Channel{local_po}",
                "peer": spine["hostname"],
                "peer_int": f"Port-Channel{remote_po}",
                "type": "port-channel",
                "physical_interface": leaf_port,
                "spine_physical_interface": spine_port
            })
            spine["interfaces"].append({
                "name": f"Port-Channel{remote_po}",
                "peer": leaf["hostname"],
                "peer_int": f"Port-Channel{local_po}",
                "type": "port-channel",
                "physical_interface": spine_port,
                "leaf_physical_interface": leaf_port
            })
        spine_port_index += len(leaf_spine_ports)
    
    if config.get("leaf_pair") in [True, "yes", "true"]:
        inter_leaf_ports = config.get("inter_leaf_ports", [51, 52])
        inter_leaf_po = config.get("inter_leaf_port_channel_id", 600)
        for pair_idx in range(0, num_leafs, 2):
            leaf1 = devices["leafs"][pair_idx]
            leaf2 = devices["leafs"][pair_idx + 1]
            for port_idx, leaf_port_num in enumerate(inter_leaf_ports):
                leaf_port = f"Ethernet{leaf_port_num}"
                leaf1["interfaces"].append({
                    "name": f"Port-Channel{inter_leaf_po}",
                    "peer": leaf2["hostname"],
                    "peer_int": f"Port-Channel{inter_leaf_po}",
                    "type": "port-channel",
                    "physical_interface": leaf_port,
                    "peer_physical_interface": leaf_port
                })
                leaf2["interfaces"].append({
                    "name": f"Port-Channel{inter_leaf_po}",
                    "peer": leaf1["hostname"],
                    "peer_int": f"Port-Channel{inter_leaf_po}",
                    "type": "port-channel",
                    "physical_interface": leaf_port,
                    "peer_physical_interface": leaf_port
                })

# Output Manager Functions
def generate_host_vars(devices: Dict[str, List[Dict]], spines: List[str], fabric_context: str, dry_run: bool = False):
    """Generate host_vars YAML files for spines and leafs using fabric_context Jinja2 template."""
    env = Environment(loader=FileSystemLoader("templates"), trim_blocks=True, lstrip_blocks=True)
    try:
        template = env.get_template(f"{fabric_context}.j2")
    except Exception as e:
        raise ValueError(f"Failed to load template {fabric_context}.j2: {e}")
    
    if not dry_run:
        os.makedirs("host_vars", exist_ok=True)
    
    # Generate host_vars for spines
    for spine in devices["spines"]:
        interfaces = []
        for intf in spine["interfaces"]:
            # Find the remote device's loopback IP for remote_bgp_neighbor
            remote_device = next(
                (d for d in devices["leafs"] if d["hostname"] == intf["peer"]),
                None
            )
            remote_bgp_neighbor = remote_device["loopback_ip"].split("/")[0] if remote_device else "-"
            
            interfaces.append({
                "local_port": intf["physical_interface"].replace("Ethernet", ""),
                "remote_host": intf["peer"],
                "remote_port": intf["leaf_physical_interface"].replace("Ethernet", ""),
                "local_po": intf["name"].replace("Port-Channel", ""),
                "remote_po": intf["peer_int"].replace("Port-Channel", ""),
                "transit_subnet": intf["transit_subnet"],
                "remote_bgp_neighbor": remote_bgp_neighbor
            })
        
        host_vars_data = {
            "local_host": spine["hostname"],
            "interfaces": interfaces,
            "bgp_neighbors": []  # Spines have no BGP neighbors in this setup
        }
        
        rendered = template.render(**host_vars_data)
        if not dry_run:
            with open(f"host_vars/{spine['hostname']}.yml", "w") as f:
                f.write(rendered)
        else:
            print(f"\nDry run: Would generate host_vars/{spine['hostname']}.yml:")
            print(rendered)
    
    # Generate host_vars for leafs
    for leaf in devices["leafs"]:
        interfaces = []
        for intf in leaf["interfaces"]:
            # Find the remote device's loopback IP for remote_bgp_neighbor
            remote_device = next(
                (d for d in devices["spines"] + devices["leafs"] if d["hostname"] == intf["peer"]),
                None
            )
            remote_bgp_neighbor = remote_device["loopback_ip"].split("/")[0] if remote_device else "-"
            
            if intf["peer"] in spines:
                interfaces.append({
                    "local_port": intf["physical_interface"].replace("Ethernet", ""),
                    "remote_host": intf["peer"],
                    "remote_port": intf["spine_physical_interface"].replace("Ethernet", ""),
                    "local_po": intf["name"].replace("Port-Channel", ""),
                    "remote_po": intf["peer_int"].replace("Port-Channel", ""),
                    "transit_subnet": intf["transit_subnet"],
                    "remote_bgp_neighbor": remote_bgp_neighbor
                })
            elif intf["peer"] in [l["hostname"] for l in devices["leafs"]] and leaf["pair_id"] is not None:
                interfaces.append({
                    "local_port": intf["physical_interface"].replace("Ethernet", ""),
                    "remote_host": intf["peer"],
                    "remote_port": intf["peer_physical_interface"].replace("Ethernet", ""),
                    "local_po": intf["name"].replace("Port-Channel", ""),
                    "remote_po": intf["peer_int"].replace("Port-Channel", ""),
                    "transit_subnet": intf["transit_subnet"],
                    "remote_bgp_neighbor": remote_bgp_neighbor
                })
        
        host_vars_data = {
            "local_host": leaf["hostname"],
            "interfaces": interfaces,
            "bgp_neighbors": [
                {"hostname": neighbor["hostname"], "neighbor_ip": neighbor["neighbor_ip"], "remote_asn": neighbor["remote_asn"]}
                for neighbor in leaf["bgp_neighbors"]
            ]
        }
        
        rendered = template.render(**host_vars_data)
        if not dry_run:
            with open(f"host_vars/{leaf['hostname']}.yml", "w") as f:
                f.write(rendered)
        else:
            print(f"\nDry run: Would generate host_vars/{leaf['hostname']}.yml:")
            print(rendered)

def generate_inventory(devices: Dict[str, List[Dict]], dry_run: bool = False):
    """Generate Ansible inventory hosts.yml without bgp_asn."""
    inventory = {
        "all": {
            "children": {
                "spines": {
                    "hosts": {
                        spine["hostname"]: {
                            "ansible_host": spine["loopback_ip"].split("/")[0]
                        }
                        for spine in devices["spines"]
                    }
                },
                "leafs": {
                    "hosts": {
                        leaf["hostname"]: {
                            "ansible_host": leaf["loopback_ip"].split("/")[0]
                        }
                        for leaf in devices["leafs"]
                    }
                }
            }
        }
    }
    
    if dry_run:
        print("\nDry run: Would generate hosts.yml:")
        print(yaml.safe_dump(inventory, sort_keys=False))
        return
    
    with open("hosts.yml", "w") as f:
        yaml.safe_dump(inventory, f, sort_keys=False)
    print("\nSaved Ansible inventory to hosts.yml")

def generate_ip_assignments_csv(devices: Dict[str, List[Dict]], dry_run: bool = False):
    """Generate ip_assignments.csv for device and endpoint IP assignments."""
    headers = ["Device", "Role", "Loopback IP", "Interface", "Transit IP", "Remote Device", "Remote Transit IP", "Transit Subnet"]
    table = [headers, ["-" * 20, "-" * 10, "-" * 15, "-" * 20, "-" * 15, "-" * 20, "-" * 15, "-" * 15]]
    
    for device in devices["spines"] + devices["leafs"]:
        # Add a row for the device itself (loopback only, no interface)
        table.append([
            device["hostname"],
            device["role"],
            device["loopback_ip"],
            "-",
            "-",
            "-",
            "-",
            "-"
        ])
        
        # Add rows for each interface
        for intf in device["interfaces"]:
            remote_device = next(
                (d for d in devices["spines"] + devices["leafs"] if d["hostname"] == intf["peer"]),
                None
            )
            if not remote_device:
                continue
            # Find the remote interface's transit IP
            remote_int = next(
                (ri for ri in remote_device["interfaces"] if ri["peer"] == device["hostname"] and ri["transit_subnet"] == intf["transit_subnet"]),
                None
            )
            remote_transit_ip = remote_int["ip"] if remote_int else "-"
            
            table.append([
                device["hostname"],
                device["role"],
                device["loopback_ip"],
                intf["physical_interface"],
                intf.get("ip", "-"),
                intf["peer"],
                remote_transit_ip,
                intf.get("transit_subnet", "-")
            ])
    
    if dry_run:
        print("\nDry run: Would generate ip_assignments.csv with the following content:")
        for row in table:
            print(f"{','.join(row)}")
        return
    
    with open("ip_assignments.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(table)
    print("\nSaved IP assignments to ip_assignments.csv")

def display_table(devices: Dict[str, List[Dict]], spines: List[str]) -> List[List[str]]:
    """Display IP and port connections in a table and return table data."""
    headers = ["Host", "Local Port", "Remote Host", "Remote Port", "Local PO", "Remote PO", "Transit Subnet", "Remote BGP Neighbor"]
    table = [headers, ["-" * 20, "-" * 12, "-" * 20, "-" * 12, "-" * 10, "-" * 10, "-" * 15, "-" * 20]]
    
    for leaf in devices["leafs"]:
        for intf in leaf["interfaces"]:
            # Find the remote device's loopback IP for remote_bgp_neighbor
            remote_device = next(
                (d for d in devices["spines"] + devices["leafs"] if d["hostname"] == intf["peer"]),
                None
            )
            remote_bgp_neighbor = remote_device["loopback_ip"].split("/")[0] if remote_device else "-"
            
            if intf["peer"] in spines:
                row = [
                    leaf["hostname"],
                    intf["physical_interface"].replace("Ethernet", ""),
                    intf["peer"],
                    intf["spine_physical_interface"].replace("Ethernet", ""),
                    intf["name"].replace("Port-Channel", ""),
                    intf["peer_int"].replace("Port-Channel", ""),
                    intf["transit_subnet"],
                    remote_bgp_neighbor
                ]
                table.append(row)
            elif intf["peer"] in [l["hostname"] for l in devices["leafs"]] and leaf["pair_id"] is not None:
                row = [
                    leaf["hostname"],
                    intf["physical_interface"].replace("Ethernet", ""),
                    intf["peer"],
                    intf["peer_physical_interface"].replace("Ethernet", ""),
                    intf["name"].replace("Port-Channel", ""),
                    intf["peer_int"].replace("Port-Channel", ""),
                    intf["transit_subnet"],
                    remote_bgp_neighbor
                ]
                table.append(row)
    
    print("\nIP and Port Connections Table:")
    for row in table:
        print(f"{row[0]:<20} | {row[1]:<12} | {row[2]:<20} | {row[3]:<12} | {row[4]:<10} | {row[5]:<10} | {row[6]:<15} | {row[7]:<20}")
    
    return table

def save_table_csv(table: List[List[str]], dry_run: bool = False):
    """Save table to connections.csv."""
    if dry_run:
        print("\nDry run: Would generate connections.csv with the following content:")
        for row in table:
            print(f"{','.join(row)}")
        return
    
    with open("connections.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(table)
    print("\nSaved table to connections.csv")

# Main Script
def get_user_input(input_path: Optional[str] = None) -> tuple[Dict[str, Any], List[str], List[str]]:
    """Get user input interactively or from input.yml."""
    if input_path:
        try:
            config = read_config("config/fabric.yml", input_path)
            # Required inputs must be present in input.yml or will prompt
            required_inputs = ["num_of_spines", "num_of_leafs", "spine_hostnames", "leaf_hostnames", "fabric_context"]
            for key in required_inputs:
                if key not in config:
                    raise ValueError(f"Missing required input: {key}")
            spines = parse_hostname_range(config["spine_hostnames"])
            leafs = parse_hostname_range(config["leaf_hostnames"])
            # Parse port ranges from input.yml if provided
            if "spine_port_channel_range" in config:
                config["spine_port_channel_range"] = parse_flexible_range(config["spine_port_channel_range"])
            if "spine_ports_range" in config:
                config["spine_ports_range"] = parse_flexible_range(config["spine_ports_range"])
            return config, spines, leafs
        except (FileNotFoundError, ValueError) as e:
            print(f"Error reading input.yml: {e}")
            print("Falling back to interactive input.")
    
    # Interactive input
    config = {}
    
    fabric_context = input("Enter fabric context (e.g., seti_oob_access): ")
    config["fabric_context"] = fabric_context
    config = read_config("config/fabric.yml", user_inputs=config)
    
    num_spines = int(input("Enter number of spines: "))
    num_leafs = int(input("Enter number of leafs: "))
    spine_hostnames = input("Enter spine hostnames (e.g., nj01pdmr[101-102]): ")
    leaf_hostnames = input("Enter leaf hostnames (e.g., nj01pamr[101-106]): ")
    
    spines = parse_hostname_range(spine_hostnames)
    leafs = parse_hostname_range(leaf_hostnames)
    
    config.update({
        "num_of_spines": num_spines,
        "num_of_leafs": num_leafs,
        "spine_hostnames": spine_hostnames,
        "leaf_hostnames": leaf_hostnames
    })
    
    # Optional inputs
    transit = input("Enter transit network (e.g., 10.10.10.0/24) [optional, press Enter to skip]: ")
    if transit:
        config["transit"] = transit
    
    loopback = input("Enter loopback network (e.g., 10.10.20.0/24) [optional, press Enter to skip]: ")
    if loopback:
        config["loopback"] = loopback
    
    bgp_asn = input("Enter BGP ASN (e.g., 65201) [optional, press Enter to skip]: ")
    if bgp_asn:
        config["bgp_asn"] = int(bgp_asn)
    
    spine_port_channel_range = input("Enter spine port channel range (e.g., [1,3,5-10]) [optional, press Enter to skip]: ")
    if spine_port_channel_range:
        config["spine_port_channel_range"] = parse_flexible_range(spine_port_channel_range)
    
    spine_ports_range = input("Enter spine ports range (e.g., [1-4,7,9-12]) [optional, press Enter to skip]: ")
    if spine_ports_range:
        config["spine_ports_range"] = parse_flexible_range(spine_ports_range)
    
    return config, spines, leafs

def main():
    """Main function."""
    parser = argparse.ArgumentParser(description="Generate fabric configuration.")
    parser.add_argument("--input", help="Path to input.yml file", default="config/input.yml")
    parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without writing files")
    args = parser.parse_args()
    
    try:
        config, spines, leafs = get_user_input(args.input)
        validate_input(config["num_of_spines"], config["num_of_leafs"], spines, leafs, config)
        
        devices = initialize_devices(config["num_of_spines"], config["num_of_leafs"], spines, leafs, config)
        build_connectivity(devices, config["num_of_leafs"], config)
        assign_ips(devices, config["num_of_leafs"], config)
        
        table = display_table(devices, spines)
        save_table_csv(table, args.dry_run)
        generate_host_vars(devices, spines, config["fabric_context"], args.dry_run)
        generate_inventory(devices, args.dry_run)
        generate_ip_assignments_csv(devices, args.dry_run)
        
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}")
        exit(1)

if __name__ == "__main__":
    main()