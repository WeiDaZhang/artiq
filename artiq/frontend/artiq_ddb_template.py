#!/usr/bin/env python3

import argparse
import sys
import json
import textwrap
from collections import defaultdict
from itertools import count

from artiq import __version__ as artiq_version


def process_header(output, description):
    if description["target"] != "kasli":
        raise NotImplementedError

    print(textwrap.dedent("""
        # Autogenerated for the {variant} variant
        core_addr = "{core_addr}"

        device_db = {{
            "core": {{
                "type": "local",
                "module": "artiq.coredevice.core",
                "class": "Core",
                "arguments": {{"host": core_addr, "ref_period": {ref_period}}}
            }},
            "core_log": {{
                "type": "controller",
                "host": "::1",
                "port": 1068,
                "command": "aqctl_corelog -p {{port}} --bind {{bind}} " + core_addr
            }},
            "core_cache": {{
                "type": "local",
                "module": "artiq.coredevice.cache",
                "class": "CoreCache"
            }},
            "core_dma": {{
                "type": "local",
                "module": "artiq.coredevice.dma",
                "class": "CoreDMA"
            }},

            "i2c_switch0": {{
                "type": "local",
                "module": "artiq.coredevice.i2c",
                "class": "PCA9548",
                "arguments": {{"address": 0xe0}}
            }},
            "i2c_switch1": {{
                "type": "local",
                "module": "artiq.coredevice.i2c",
                "class": "PCA9548",
                "arguments": {{"address": 0xe2}}
            }},
        }}
        """).format(
            variant=description["variant"],
            core_addr=description.get("core_addr", "192.168.1.70"),
            ref_period=1/(8*description.get("rtio_frequency", 125e6))),
        file=output)


class PeripheralManager:
    def __init__(self, output, master_description):
        self.counts = defaultdict(int)
        self.output = output
        self.master_description = master_description

    def get_name(self, ty):
        count = self.counts[ty]
        self.counts[ty] = count + 1
        return "{}{}".format(ty, count)

    def gen(self, string, **kwargs):
        print(textwrap.dedent(string).format(**kwargs), file=self.output)

    def process_dio(self, rtio_offset, peripheral):
        class_names = {
            "input": "TTLInOut",
            "output": "TTLOut"
        }
        classes = [
            class_names[peripheral["bank_direction_low"]],
            class_names[peripheral["bank_direction_high"]]
        ]
        channel = count(0)
        for i in range(8):
            self.gen("""
                device_db["{name}"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.ttl",
                    "class": "{class_name}",
                    "arguments": {{"channel": 0x{channel:06x}}},
                }}
                """,
                name=self.get_name("ttl"),
                class_name=classes[i//4],
                channel=rtio_offset+next(channel))
        if peripheral.get("edge_counter", False):
            for i in range(8):
                class_name = classes[i//4]
                if class_name == "TTLInOut":
                    self.gen("""
                        device_db["{name}"] = {{
                            "type": "local",
                            "module": "artiq.coredevice.edge_counter",
                            "class": "EdgeCounter",
                            "arguments": {{"channel": 0x{channel:06x}}},
                        }}
                        """,
                        name=self.get_name("ttl_counter"),
                        channel=rtio_offset+next(channel))
        return next(channel)

    def process_urukul(self, rtio_offset, peripheral):
        urukul_name = self.get_name("urukul")
        synchronization = peripheral.get("synchronization", False)
        channel = count(0)
        self.gen("""
            device_db["eeprom_{name}"]={{
                "type": "local",
                "module": "artiq.coredevice.kasli_i2c",
                "class": "KasliEEPROM",
                "arguments": {{"port": "EEM{eem}"}}
            }}

            device_db["spi_{name}"]={{
                "type": "local",
                "module": "artiq.coredevice.spi2",
                "class": "SPIMaster",
                "arguments": {{"channel": 0x{channel:06x}}}
            }}""",
            name=urukul_name,
            eem=peripheral["ports"][0],
            channel=rtio_offset+next(channel))
        if synchronization:
            self.gen("""
                device_db["ttl_{name}_sync"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.ttl",
                    "class": "TTLClockGen",
                    "arguments": {{"channel": 0x{channel:06x}, "acc_width": 4}}
                }}""",
                name=urukul_name,
                channel=rtio_offset+next(channel))
        self.gen("""
            device_db["ttl_{name}_io_update"] = {{
                "type": "local",
                "module": "artiq.coredevice.ttl",
                "class": "TTLOut",
                "arguments": {{"channel": 0x{channel:06x}}}
            }}""",
            name=urukul_name,
            channel=rtio_offset+next(channel))
        if len(peripheral["ports"]) > 1:
            for i in range(4):
                self.gen("""
                    device_db["ttl_{name}_sw{uchn}"] = {{
                        "type": "local",
                        "module": "artiq.coredevice.ttl",
                        "class": "TTLOut",
                        "arguments": {{"channel": 0x{channel:06x}}}
                    }}""",
                    name=urukul_name,
                    uchn=i,
                    channel=rtio_offset+next(channel))
        self.gen("""
            device_db["{name}_cpld"] = {{
                "type": "local",
                "module": "artiq.coredevice.urukul",
                "class": "CPLD",
                "arguments": {{
                    "spi_device": "spi_{name}",
                    "sync_device": {sync_device},
                    "io_update_device": "ttl_{name}_io_update",
                    "refclk": {refclk},
                    "clk_sel": {clk_sel}
                }}
            }}""",
            name=urukul_name,
            sync_device="\"ttl_{name}_sync\"".format(name=urukul_name) if synchronization else "None",
            refclk=peripheral.get("refclk", self.master_description.get("rtio_frequency", 125e6)),
            clk_sel=peripheral["clk_sel"])
        dds = peripheral.get("dds", "ad9910")
        pll_vco = peripheral.get("pll_vco", None)
        for i in range(4):
            if dds == "ad9910":
                self.gen("""
                    device_db["{name}_ch{uchn}"] = {{
                        "type": "local",
                        "module": "artiq.coredevice.ad9910",
                        "class": "AD9910",
                        "arguments": {{
                            "pll_n": {pll_n},
                            "chip_select": {chip_select},
                            "cpld_device": "{name}_cpld"{sw}{pll_vco}{sync_delay_seed}{io_update_delay}
                        }}
                    }}""",
                    name=urukul_name,
                    chip_select=4 + i,
                    uchn=i,
                    sw=",\n        \"sw_device\": \"ttl_{name}_sw{uchn}\"".format(name=urukul_name, uchn=i) if len(peripheral["ports"]) > 1 else "",
                    pll_vco=",\n        \"pll_vco\": {}".format(pll_vco) if pll_vco is not None else "",
                    pll_n=peripheral.get("pll_n", 32),
                    sync_delay_seed=",\n        \"sync_delay_seed\": \"eeprom_{}:{}\"".format(urukul_name, 64 + 4*i) if synchronization else "",
                    io_update_delay=",\n        \"io_update_delay\": \"eeprom_{}:{}\"".format(urukul_name, 64 + 4*i) if synchronization else "")
            elif dds == "ad9912":
                self.gen("""
                    device_db["{name}_ch{uchn}"] = {{
                        "type": "local",
                        "module": "artiq.coredevice.ad9912",
                        "class": "AD9912",
                        "arguments": {{
                            "pll_n": {pll_n},
                            "chip_select": {chip_select},
                            "cpld_device": "{name}_cpld"{sw}{pll_vco}
                        }}
                    }}""",
                    name=urukul_name,
                    chip_select=4 + i,
                    uchn=i,
                    sw=",\n        \"sw_device\": \"ttl_{name}_sw{uchn}\"".format(name=urukul_name, uchn=i) if len(peripheral["ports"]) > 1 else "",
                    pll_vco=",\n        \"pll_vco\": {}".format(pll_vco) if pll_vco is not None else "",
                    pll_n=peripheral.get("pll_n", 8))
            else:
                raise ValueError
        return next(channel)

    def process_mirny(self, rtio_offset, peripheral):
        mirny_name = self.get_name("mirny")
        channel = count(0)
        self.gen("""
           device_db["spi_{name}"]={{
               "type": "local",
               "module": "artiq.coredevice.spi2",
               "class": "SPIMaster",
               "arguments": {{"channel": 0x{channel:06x}}}
           }}""",
            name=mirny_name,
            channel=rtio_offset+next(channel))

        for i in range(4):
            self.gen("""
                device_db["ttl_{name}_sw{mchn}"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.ttl",
                    "class": "TTLOut",
                    "arguments": {{"channel": 0x{ttl_channel:06x}}}
                }}""",
                name=mirny_name,
                mchn=i,
                ttl_channel=rtio_offset+next(channel))

        for i in range(4):
            self.gen("""
                device_db["{name}_ch{mchn}"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.adf5355",
                    "class": "ADF5355",
                    "arguments": {{
                        "channel": {mchn},
                        "sw_device": "ttl_{name}_sw{mchn}",
                        "cpld_device": "{name}_cpld",
                    }}
                }}""",
                name=mirny_name,
                mchn=i)

        self.gen("""
            device_db["{name}_cpld"] = {{
                "type": "local",
                "module": "artiq.coredevice.mirny",
                "class": "Mirny",
                "arguments": {{"spi_device": "spi_{name}"}},
            }}""",
            name=mirny_name)

        return next(channel)

    def process_novogorny(self, rtio_offset, peripheral):
        self.gen("""
            device_db["spi_{name}_adc"] = {{
                "type": "local",
                "module": "artiq.coredevice.spi2",
                "class": "SPIMaster",
                "arguments": {{"channel": 0x{adc_channel:06x}}}
            }}
            device_db["ttl_{name}_cnv"] = {{
                "type": "local",
                "module": "artiq.coredevice.ttl",
                "class": "TTLOut",
                "arguments": {{"channel": 0x{cnv_channel:06x}}},
            }}
            device_db["{name}"] = {{
                "type": "local",
                "module": "artiq.coredevice.novogorny",
                "class": "Novogorny",
                "arguments": {{
                    "spi_adc_device": "spi_{name}_adc",
                    "cnv_device": "ttl_{name}_cnv"
                }}
            }}""",
            name=self.get_name("novogorny"),
            adc_channel=rtio_offset,
            cnv_channel=rtio_offset + 1)
        return 2

    def process_sampler(self, rtio_offset, peripheral):
        self.gen("""
            device_db["spi_{name}_adc"] = {{
                "type": "local",
                "module": "artiq.coredevice.spi2",
                "class": "SPIMaster",
                "arguments": {{"channel": 0x{adc_channel:06x}}}
            }}
            device_db["spi_{name}_pgia"] = {{
                "type": "local",
                "module": "artiq.coredevice.spi2",
                "class": "SPIMaster",
                "arguments": {{"channel": 0x{pgia_channel:06x}}}
            }}
            device_db["ttl_{name}_cnv"] = {{
                "type": "local",
                "module": "artiq.coredevice.ttl",
                "class": "TTLOut",
                "arguments": {{"channel": 0x{cnv_channel:06x}}},
            }}
            device_db["{name}"] = {{
                "type": "local",
                "module": "artiq.coredevice.sampler",
                "class": "Sampler",
                "arguments": {{
                    "spi_adc_device": "spi_{name}_adc",
                    "spi_pgia_device": "spi_{name}_pgia",
                    "cnv_device": "ttl_{name}_cnv"
                }}
            }}""",
            name=self.get_name("sampler"),
            adc_channel=rtio_offset,
            pgia_channel=rtio_offset + 1,
            cnv_channel=rtio_offset + 2)
        return 3

    def process_suservo(self, rtio_offset, peripheral):
        suservo_name = self.get_name("suservo")
        sampler_name = self.get_name("sampler")
        urukul0_name = self.get_name("urukul")
        urukul1_name = self.get_name("urukul")
        channel = count(0)
        for i in range(8):
            self.gen("""
                device_db["{suservo_name}_ch{suservo_chn}"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.suservo",
                    "class": "Channel",
                    "arguments": {{"channel": 0x{suservo_channel:06x}, "servo_device": "{suservo_name}"}}
                }}""",
                suservo_name=suservo_name,
                suservo_chn=i,
                suservo_channel=rtio_offset+next(channel))
        self.gen("""
            device_db["{suservo_name}"] = {{
                "type": "local",
                "module": "artiq.coredevice.suservo",
                "class": "SUServo",
                "arguments": {{
                    "channel": 0x{suservo_channel:06x},
                    "pgia_device": "spi_{sampler_name}_pgia",
                    "cpld0_device": "{urukul0_name}_cpld",
                    "cpld1_device": "{urukul1_name}_cpld",
                    "dds0_device": "{urukul0_name}_dds",
                    "dds1_device": "{urukul1_name}_dds"
                }}
            }}""",
            suservo_name=suservo_name,
            sampler_name=sampler_name,
            urukul0_name=urukul0_name,
            urukul1_name=urukul1_name,
            suservo_channel=rtio_offset+next(channel))
        self.gen("""
            device_db["spi_{sampler_name}_pgia"] = {{
                "type": "local",
                "module": "artiq.coredevice.spi2",
                "class": "SPIMaster",
                "arguments": {{"channel": 0x{sampler_channel:06x}}}
            }}""",
            sampler_name=sampler_name,
            sampler_channel=rtio_offset+next(channel))
        pll_vco = peripheral.get("pll_vco", None)
        for urukul_name in (urukul0_name, urukul1_name):
            self.gen("""
                device_db["spi_{urukul_name}"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.spi2",
                    "class": "SPIMaster",
                    "arguments": {{"channel": 0x{urukul_channel:06x}}}
                }}
                device_db["{urukul_name}_cpld"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.urukul",
                    "class": "CPLD",
                    "arguments": {{
                        "spi_device": "spi_{urukul_name}",
                        "refclk": {refclk},
                        "clk_sel": {clk_sel}
                    }}
                }}
                device_db["{urukul_name}_dds"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.ad9910",
                    "class": "AD9910",
                    "arguments": {{
                        "pll_n": {pll_n},
                        "chip_select": 3,
                        "cpld_device": "{urukul_name}_cpld"{pll_vco}
                    }}
                }}""",
                urukul_name=urukul_name,
                urukul_channel=rtio_offset+next(channel),
                refclk=peripheral.get("refclk", self.master_description.get("rtio_frequency", 125e6)),
                clk_sel=peripheral["clk_sel"],
                pll_vco=",\n        \"pll_vco\": {}".format(pll_vco) if pll_vco is not None else "",
                pll_n=peripheral.get("pll_n", 32))
        return next(channel)

    def process_zotino(self, rtio_offset, peripheral):
        self.gen("""
            device_db["spi_{name}"] = {{
                "type": "local",
                "module": "artiq.coredevice.spi2",
                "class": "SPIMaster",
                "arguments": {{"channel": 0x{spi_channel:06x}}}
            }}
            device_db["ttl_{name}_ldac"] = {{
                "type": "local",
                "module": "artiq.coredevice.ttl",
                "class": "TTLOut",
                "arguments": {{"channel": 0x{ldac_channel:06x}}}
            }}
            device_db["ttl_{name}_clr"] = {{
                "type": "local",
                "module": "artiq.coredevice.ttl",
                "class": "TTLOut",
                "arguments": {{"channel": 0x{clr_channel:06x}}}
            }}
            device_db["{name}"] = {{
                "type": "local",
                "module": "artiq.coredevice.zotino",
                "class": "Zotino",
                "arguments": {{
                    "spi_device": "spi_{name}",
                    "ldac_device": "ttl_{name}_ldac",
                    "clr_device": "ttl_{name}_clr"
                }}
            }}""",
            name=self.get_name("zotino"),
            spi_channel=rtio_offset,
            ldac_channel=rtio_offset + 1,
            clr_channel=rtio_offset + 2)
        return 3

    def process_grabber(self, rtio_offset, peripheral):
        self.gen("""
            device_db["{name}"] = {{
                "type": "local",
                "module": "artiq.coredevice.grabber",
                "class": "Grabber",
                "arguments": {{"channel_base": 0x{channel:06x}}}
            }}""",
            name=self.get_name("grabber"),
            channel=rtio_offset)
        return 2

    def process_fastino(self, rtio_offset, peripheral):
        self.gen("""
            device_db["{name}"] = {{
                "type": "local",
                "module": "artiq.coredevice.fastino",
                "class": "Fastino",
                "arguments": {{"channel": 0x{channel:06x}}}
            }}""",
            name=self.get_name("fastino"),
            channel=rtio_offset)
        return 1

    def process(self, rtio_offset, peripheral):
        processor = getattr(self, "process_"+str(peripheral["type"]))
        return processor(rtio_offset, peripheral)

    def add_sfp_leds(self, rtio_offset):
        for i in range(2):
            self.gen("""
                device_db["{name}"] = {{
                    "type": "local",
                    "module": "artiq.coredevice.ttl",
                    "class": "TTLOut",
                    "arguments": {{"channel": 0x{channel:06x}}}
                }}""",
                name=self.get_name("led"),
                channel=rtio_offset+i)
        return 2


def process(output, master_description, satellites):
    base = master_description["base"]
    if base not in ("standalone", "master"):
        raise ValueError("Invalid master base")

    if base == "standalone" and satellites:
        raise ValueError("A standalone system cannot have satellites")

    process_header(output, master_description)

    pm = PeripheralManager(output, master_description)

    print("# {} peripherals".format(base), file=output)
    rtio_offset = 0
    for peripheral in master_description["peripherals"]:
        n_channels = pm.process(rtio_offset, peripheral)
        rtio_offset += n_channels
    if base == "standalone":
        n_channels = pm.add_sfp_leds(rtio_offset)
        rtio_offset += n_channels

    for destination, description in satellites:
        if description["base"] != "satellite":
            raise ValueError("Invalid base for satellite at destination {}".format(destination))

        print("# DEST#{} peripherals".format(destination), file=output)
        rtio_offset = destination << 16
        for peripheral in description["peripherals"]:
            n_channels = pm.process(rtio_offset, peripheral)
            rtio_offset += n_channels


def main():
    parser = argparse.ArgumentParser(
        description="ARTIQ device database template builder")
    parser.add_argument("--version", action="version",
                        version="ARTIQ v{}".format(artiq_version),
                        help="print the ARTIQ version number")
    parser.add_argument("master_description", metavar="MASTER_DESCRIPTION",
                        help="JSON system description file for the standalone or master node")
    parser.add_argument("-o", "--output",
                        help="output file, defaults to standard output if omitted")
    parser.add_argument("-s", "--satellite", nargs=2, action="append",
                        default=[], metavar=("DESTINATION", "DESCRIPTION"), type=str,
                        help="add DRTIO satellite at the given destination number with "
                             "devices from the given JSON description")

    args = parser.parse_args()

    with open(args.master_description, "r") as f:
        master_description = json.load(f)

    satellites = []
    for destination, description in args.satellite:
        with open(description, "r") as f:
            satellites.append((int(destination, 0), json.load(f)))

    if args.output is not None:
        with open(args.output, "w") as f:
            process(f, master_description, satellites)
    else:
        process(sys.stdout, master_description, satellites)


if __name__ == "__main__":
    main()
