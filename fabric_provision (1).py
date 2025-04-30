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
    config = _read_yaml(config_path, "fabric configuration")
    
    if input_path:
        input_config = _read_yaml(input_path, "input configuration")
        config.update(input_config)
    
    if user_inputs:
        config.update(user_inputs)
    
    # Apply default ranges if not provided
    if "spine_port_channel_range" not in config:
        config["spine_port_channel_range"] = list(range(1, 21))  # Default: [1-20]
    if "spine_ports_range" not in config:
        config["spine_ports_range"] = list(range(1, 41))  # Default: [1-40]
    
    _validate_config(config)
    return config

def _read_yaml(file_path: str, file_type: str) -> Dict[str, Any]:
    """Read YAML file."""
    try:
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        if not data:
            raise ValueError(f"{file_type.capitalize()} file is empty")
        return data
    except FileNotFoundError:
        raise ValueError(f"{file_type.capitalize()} file not found: {file_path}")
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML format in {file_type} file: {e}")

def _validate_config(config: Dict[str, Any]):
    """Validate configuration settings."""
    required_keys = [
        "num_of_spines", "num_of_leafs", "transit", "loopback", "bgp_asn",
        "leaf_spine_linknet", "leaf_spine_ports", "leaf_pair",
        "leaf_spine_port_channel_id", "spine_port_channel_range", "spine_ports_range",
        "inter_leaf", "inter_leaf_ports", "inter_leaf_port_channel_id", "inter_leaf_linknet"
    ]
    for key in required_keys:
        if key not in config:
            raise ValueError(f"Missing required configuration key: {key}")
    
    try:
        transit_net = ipaddress.ip_network(config["transit"], strict=False)
        loopback_net = ipaddress.ip_network(config["loopback"], strict=False)
        if transit_net.overlaps(loopback_net):
            raise ValueError("Transit and loopback networks overlap")
    except ValueError as e:
        raise ValueError(f"Invalid network address: {e}")

    if not isinstance(config["num_of_spines"], int) or config["num_of_spines"] < 1:
        raise ValueError("num_of_spines must be a positive integer")
    if not isinstance(config["num_of_leafs"], int) or config["num_of_leafs"] < 1 or config["num_of_leafs"] % 2 != 0:
        raise ValueError("num_of_leafs must be a positive even integer")
    if not isinstance(config["bgp_asn"], int) or config["bgp_asn"] < 1:
        raise ValueError("bgp_asn must be a positive integer")

    if config.get("leaf_spine_link_agg", False) and len(config["leaf_spine_ports"]) != 2:
        raise ValueError("Leaf-spine link aggregation requires exactly 2 ports")
    if config["leaf_pair"] == "yes" and config.get("inter_leaf_link_agg", False) and len(config["inter_leaf_ports"]) != 2:
        raise ValueError("Inter-leaf link aggregation requires exactly 2 ports")

    # Validate spine port ranges
    if not isinstance(config["spine_port_channel_range"], list) or not config["spine_port_channel_range"]:
        raise ValueError("spine_port_channel_range must be a non-empty list")
    if not isinstance(config["spine_ports_range"], list) or not config["spine_ports_range"]:
        raise ValueError("spine_ports_range must be a non-empty list")
    if len(config["spine_ports_range"]) < config["num_of_leafs"] * len(config["leaf_spine_ports"]):
        raise ValueError("spine_ports_range does not have enough ports for all leaf connections")
    
    # Validate port numbers
    for port in config["spine_port_channel_range"] + config["spine_ports_range"]:
        if not isinstance(port, int) or port < 1:
            raise ValueError(f"Port numbers must be positive integers: {port}")
    if len(set(config["spine_port_channel_range"])) != len(config["spine_port_channel_range"]):
        raise ValueError("Duplicate port numbers in spine_port_channel_range")
    if len(set(config["spine_ports_range"])) != len(config["spine_ports_range"]):
        raise ValueError("Duplicate port numbers in spine_ports_range")

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

def parse_flexible_range(range_input: str) -> List[int]:
    """Parse flexible range format (e.g., [1,3,5-10])."""
    if not range_input.startswith("[") or not range_input.endswith("]"):
        raise ValueError(f"Invalid range format: {range_input}. Expected format: [1,3,5-10]")
    
    range_input = range_input[1:-1].strip()
    if not range_input:
        return []
    
    result = set()
    items = range_input.split(",")
    
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
                    raise ValueError(f"Port numbers must be positive integers: {num}")
                result.add(num)
            except ValueError:
                raise ValueError(f"Invalid number format: {item}")
    
    return sorted(list(result))

def validate_input(num_spines: int, num_leafs: int, spines: List[str], leafs: List[str]):
    """Validate user inputs."""
    if num_spines < 1:
        raise ValueError("Number of spines must be greater than 0")
    if num_leafs < 1 or num_leafs % 2 != 0:
        raise ValueError("Number of leafs must be even and greater than 0")
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
def load_state(state_file: str = "state.yml") -> Dict[str, Any]:
    """Load state from state.yml if it exists."""
    if os.path.exists(state_file):
        try:
            with open(state_file, 'r') as f:
                return yaml.safe_load(f) or {}
        except yaml.YAMLError:
            return {}
    return {}

def save_state(state: Dict[str, Any], state_file: str = "state.yml"):
    """Save state to state.yml."""
    with open(state_file, 'w') as f:
        yaml.safe_dump(state, f, sort_keys=False)

def generate_subnets(network: str, prefix_len: str) -> List[ipaddress.IPv4Network]:
    """Generate list of subnets from a network."""
    net = ipaddress.ip_network(network)
    return list(net.subnets(new_prefix=int(prefix_len.lstrip('/'))))

def generate_ips(network: str, start: int, end: int) -> List[str]:
    """Generate list of IPs from a network."""
    net = ipaddress.ip_network(network)
    return [str(ip) for ip in list(net)[start:end+1]]

def assign_ips(devices: Dict[str, List[Dict]], num_leafs: int, config: Dict[str, Any], 
               state: Dict[str, Any]) -> Dict[str, Any]:
    """Assign IPs with state persistence."""
    transit_subnets = generate_subnets(config["transit"], config["leaf_spine_linknet"])
    loopback_ips = generate_ips(config["loopback"], 1, 255)
    state = state.copy()
    
    if "loopbacks" not in state:
        state["loopbacks"] = {}
    if "transit" not in state:
        state["transit"] = {}
    if "inter_leaf" not in state:
        state["inter_leaf"] = {}
    
    for spine_idx, spine in enumerate(devices["spines"]):
        hostname = spine["hostname"]
        if hostname in state["loopbacks"]:
            spine["loopback_ip"] = state["loopbacks"][hostname]
        else:
            spine["loopback_ip"] = f"{loopback_ips[spine_idx]}/32"
            state["loopbacks"][hostname] = spine["loopback_ip"]
    
    transit_index = 0
    for leaf_idx, leaf in enumerate(devices["leafs"]):
        hostname = leaf["hostname"]
        if hostname in state["loopbacks"]:
            leaf["loopback_ip"] = state["loopbacks"][hostname]
        else:
            leaf["loopback_ip"] = f"{loopback_ips[100 + leaf_idx + 1]}/32"
            state["loopbacks"][hostname] = leaf["loopback_ip"]
        
        is_odd_leaf = leaf_idx % 2 == 0
        spine_idx = 0 if is_odd_leaf else 1
        spine = devices["spines"][spine_idx]
        leaf_key = f"{leaf['hostname']}-{spine['hostname']}"
        
        if leaf_key in state["transit"]:
            transit_net = ipaddress.ip_network(state["transit"][leaf_key]["subnet"])
            transit_ips = [str(ip) for ip in islice(transit_net, 2)]
        else:
            transit_net = transit_subnets[transit_index]
            transit_ips = [str(ip) for ip in islice(transit_net, 2)]
            state["transit"][leaf_key] = {"subnet": str(transit_net)}
        
        leaf_ints = [intf for intf in leaf["interfaces"] if intf["peer"] == spine["hostname"]]
        spine_ints = [intf for intf in spine["interfaces"] if intf["peer"] == leaf["hostname"]]
        for leaf_int in leaf_ints:
            leaf_int.update({"ip": f"{transit_ips[0]}/31", "transit_subnet": str(transit_net)})
        for spine_int in spine_ints:
            spine_int.update({"ip": f"{transit_ips[1]}/31", "transit_subnet": str(transit_net)})
        
        leaf["bgp_neighbors"] = [
            {"hostname": devices["spines"][0]["hostname"], "neighbor_ip": devices["spines"][0]["loopback_ip"], "remote_asn": config["bgp_asn"]},
            {"hostname": devices["spines"][1]["hostname"], "neighbor_ip": devices["spines"][1]["loopback_ip"], "remote_asn": config["bgp_asn"]}
        ]
        transit_index += 1
    
    if config["leaf_pair"] == "yes":
        inter_leaf_subnets = list(islice(transit_subnets, num_leafs, num_leafs + (num_leafs // 2)))
        inter_leaf_index = 0
        for pair_idx in range(0, num_leafs, 2):
            leaf1 = devices["leafs"][pair_idx]
            leaf2 = devices["leafs"][pair_idx + 1]
            pair_key = f"{leaf1['hostname']}-{leaf2['hostname']}"
            
            if pair_key in state["inter_leaf"]:
                inter_leaf_net = ipaddress.ip_network(state["inter_leaf"][pair_key]["subnet"])
                inter_leaf_ips = [str(ip) for ip in islice(inter_leaf_net, 2)]
            else:
                inter_leaf_net = inter_leaf_subnets[inter_leaf_index]
                inter_leaf_ips = [str(ip) for ip in islice(inter_leaf_net, 2)]
                state["inter_leaf"][pair_key] = {"subnet": str(inter_leaf_net)}
            
            leaf1_ints = [intf for intf in leaf1["interfaces"] if intf["peer"] == leaf2["hostname"]]
            leaf2_ints = [intf for intf in leaf2["interfaces"] if intf["peer"] == leaf1["hostname"]]
            for leaf1_int in leaf1_ints:
                leaf1_int.update({"ip": f"{inter_leaf_ips[0]}/31", "transit_subnet": str(inter_leaf_net)})
            for leaf2_int in leaf2_ints:
                leaf2_int.update({"ip": f"{inter_leaf_ips[1]}/31", "transit_subnet": str(inter_leaf_net)})
            inter_leaf_index += 1
    
    return state

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
            "bgp_asn": config["bgp_asn"],
            "loopback_ip": None,
            "bgp_neighbors": []
        })
    for i, hostname in enumerate(leafs[:num_leafs]):
        pair_id = (i // 2) + 1 if config["leaf_pair"] == "yes" else None
        devices["leafs"].append({
            "hostname": hostname,
            "role": "leaf",
            "interfaces": [],
            "bgp_asn": config["bgp_asn"],
            "pair_id": pair_id,
            "loopback_ip": None,
            "bgp_neighbors": []
        })
    return devices

def build_connectivity(devices: Dict[str, List[Dict]], num_leafs: int, config: Dict[str, Any]):
    """Build leaf-spine and inter-leaf connectivity."""
    spine_port_index = 0  # Start from index 0 to use spine_ports_range sequentially
    for leaf_idx, leaf in enumerate(devices["leafs"]):
        is_odd_leaf = leaf_idx % 2 == 0
        spine_idx = 0 if is_odd_leaf else 1
        spine = devices["spines"][spine_idx]
        local_po = config["leaf_spine_port_channel_id"]
        remote_po = config["leaf_spine_port_channel_id"]
        
        for port_idx, leaf_port_num in enumerate(config["leaf_spine_ports"]):
            leaf_port = f"Ethernet{leaf_port_num}"
            spine_port_num = config["spine_ports_range"][spine_port_index + port_idx]
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
        spine_port_index += len(config["leaf_spine_ports"])  # Increment by number of ports per leaf
    
    if config["leaf_pair"] == "yes":
        for pair_idx in range(0, num_leafs, 2):
            leaf1 = devices["leafs"][pair_idx]
            leaf2 = devices["leafs"][pair_idx + 1]
            inter_leaf_po = config["inter_leaf_port_channel_id"]
            for port_idx, leaf_port_num in enumerate(config["inter_leaf_ports"]):
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
def generate_host_vars(devices: Dict[str, List[Dict]], spines: List[str], dry_run: bool = False):
    """Generate host_vars YAML files for leafs using Jinja2 template."""
    env = Environment(loader=FileSystemLoader("templates"), trim_blocks=True, lstrip_blocks=True)
    template = env.get_template("host_vars.j2")
    
    if not dry_run:
        os.makedirs("host_vars", exist_ok=True)
    
    for leaf in devices["leafs"]:
        interfaces = []
        for intf in leaf["interfaces"]:
            if intf["peer"] in spines:
                interfaces.append({
                    "local_port": intf["physical_interface"].replace("Ethernet", ""),
                    "remote_host": intf["peer"],
                    "remote_port": intf["spine_physical_interface"].replace("Ethernet", ""),
                    "local_po": intf["name"].replace("Port-Channel", ""),
                    "remote_po": intf["peer_int"].replace("Port-Channel", ""),
                    "transit_subnet": intf["transit_subnet"],
                    "remote_bgp_neighbor": next(n["neighbor_ip"] for n in leaf["bgp_neighbors"] if n["hostname"] == intf["peer"])
                })
            elif intf["peer"] in [l["hostname"] for l in devices["leafs"]] and leaf["pair_id"] is not None:
                interfaces.append({
                    "local_port": intf["physical_interface"].replace("Ethernet", ""),
                    "remote_host": intf["peer"],
                    "remote_port": intf["peer_physical_interface"].replace("Ethernet", ""),
                    "local_po": intf["name"].replace("Port-Channel", ""),
                    "remote_po": intf["peer_int"].replace("Port-Channel", ""),
                    "transit_subnet": intf["transit_subnet"]
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

def display_table(devices: Dict[str, List[Dict]], spines: List[str]) -> List[List[str]]:
    """Display IP and port connections in a table and return table data."""
    headers = ["Host", "Local Port", "Remote Host", "Remote Port", "Local PO", "Remote PO", "Transit Subnet", "Remote BGP Neighbor"]
    table = [headers, ["-" * 20, "-" * 12, "-" * 20, "-" * 12, "-" * 10, "-" * 10, "-" * 15, "-" * 20]]
    
    for leaf in devices["leafs"]:
        for intf in leaf["interfaces"]:
            if intf["peer"] in spines:
                row = [
                    leaf["hostname"],
                    intf["physical_interface"].replace("Ethernet", ""),
                    intf["peer"],
                    intf["spine_physical_interface"].replace("Ethernet", ""),
                    intf["name"].replace("Port-Channel", ""),
                    intf["peer_int"].replace("Port-Channel", ""),
                    intf["transit_subnet"],
                    next(n["neighbor_ip"] for n in leaf["bgp_neighbors"] if n["hostname"] == intf["peer"])
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
                    "-"
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
            spines = parse_hostname_range(config["spine_hostnames"])
            leafs = parse_hostname_range(config["leaf_hostnames"])
            # Parse port ranges from input.yml if provided
            if "spine_port_channel_range" in config:
                config["spine_port_channel_range"] = parse_flexible_range(config["spine_port_channel_range"])
            if "spine_ports_range" in config:
                config["spine_ports_range"] = parse_flexible_range(config["spine_ports_range"])
            return config, spines, leafs
        except Exception as e:
            print(f"Error reading input.yml: {e}. Falling back to interactive input.")
    
    user_inputs = {}
    user_inputs["num_of_spines"] = int(input("Enter the number of spines to provision: ").strip())
    user_inputs["num_of_leafs"] = int(input("Enter the number of leafs to provision (must be even): ").strip())
    user_inputs["transit"] = input("Enter the transit network (e.g., 10.10.10.0/24): ").strip()
    user_inputs["loopback"] = input("Enter the loopback network (e.g., 10.10.20.0/24): ").strip()
    user_inputs["bgp_asn"] = int(input("Enter the BGP ASN (e.g., 65201): ").strip())
    
    # Allow empty input for defaults
    port_channel_input = input("Enter spine port channel range (e.g., [1,3,5-10], press Enter for default [1-20]): ").strip()
    ports_range_input = input("Enter spine ports range (e.g., [1-4,7,9-12], press Enter for default [1-40]): ").strip()
    
    if port_channel_input:
        user_inputs["spine_port_channel_range"] = parse_flexible_range(port_channel_input)
    if ports_range_input:
        user_inputs["spine_ports_range"] = parse_flexible_range(ports_range_input)
    
    spine_range = input("Enter spine hostname range (e.g., nj01pdmr[101-102]): ").strip()
    leaf_range = input("Enter leaf hostname range (e.g., nj01pamr[101-106]): ").strip()
    
    spines = parse_hostname_range(spine_range)
    leafs = parse_hostname_range(leaf_range)
    
    return user_inputs, spines, leafs

def main():
    """Run the fabric provisioning process."""
    parser = argparse.ArgumentParser(description="Fabric provisioning script")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry run mode (no file writes)")
    args = parser.parse_args()
    
    try:
        input_path = "config/input.yml"
        user_inputs, spines, leafs = get_user_input(input_path if not args.dry_run else None)
        
        config = read_config("config/fabric.yml", user_inputs=user_inputs)
        
        validate_input(config["num_of_spines"], config["num_of_leafs"], spines, leafs)
        
        devices = initialize_devices(config["num_of_spines"], config["num_of_leafs"], spines, leafs, config)
        
        build_connectivity(devices, config["num_of_leafs"], config)
        
        state = load_state()
        state = assign_ips(devices, config["num_of_leafs"], config, state)
        
        generate_host_vars(devices, spines, dry_run=args.dry_run)
        generate_inventory(devices, dry_run=args.dry_run)
        table = display_table(devices, spines)
        save_table_csv(table, dry_run=args.dry_run)
        
        if not args.dry_run:
            save_state(state)
        else:
            print("\nDry run: Would save state to state.yml")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()