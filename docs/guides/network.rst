.. _guide-network:

Network Faults
==============

Network faults simulate real-world link degradation — latency, packet loss,
corruption, and duplication — without touching application code.  Two
mechanisms are available:

* **tc netem** — Linux Traffic Control, manipulates packets at the kernel
  qdisc layer.  Visible to TCP (checksums are fixed).  Requires ``iproute2``
  and ``sudo``.
* **BPF / XDP** (``SilentNetworkCorrupt``) — flips bits *before* the
  checksum is recalculated, producing **silent** data corruption.  TCP
  delivers the packet intact but the payload is wrong.

.. mermaid::

   flowchart TD
       APP_N["YOUR APPLICATION"]
       KERNEL_N["LINUX KERNEL NETWORK STACK"]
       TC_N["tc netem qdisc\ndelay — add latency\nloss — drop packets\ncorrupt — flip + recheck\ndup — clone packets\nchecksum fixed → visible"]
       BPF_N["BPF / XDP hook\nflips bits BEFORE checksum\nTCP sees valid packet\npayload is silently bad\nchecksum ok → invisible"]
       NIC_N["Physical / Virtual NIC"]

       APP_N -->|"TCP/IP"| KERNEL_N
       KERNEL_N --> TC_N
       KERNEL_N --> BPF_N
       KERNEL_N --> NIC_N

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Fault
     - Effect
     - Mechanism
   * - ``NetworkDelay``
     - Add artificial RTT latency (+ optional jitter)
     - tc netem delay
   * - ``NetworkLoss``
     - Drop N % of packets
     - tc netem loss
   * - ``NetworkCorrupt``
     - Corrupt N % of packets (TCP checksum updated)
     - tc netem corrupt
   * - ``NetworkDuplicate``
     - Duplicate N % of packets
     - tc netem duplicate
   * - ``NetworkBandwidthLimit``
     - Cap interface throughput to a maximum rate
     - tc netem rate
   * - ``NetworkReorder``
     - Deliver a percentage of packets out of order
     - tc netem reorder
   * - ``NetworkReset``
     - Inject TCP RST to abruptly terminate connections
     - iptables REJECT --reject-with tcp-reset
   * - ``NetworkPartition``
     - Drop all traffic to/from a specific IP
     - iptables DROP
   * - ``SilentNetworkCorrupt``
     - Flip bits silently — TCP checksum still valid
     - BPF / XDP hook

.. note::

   All ``tc netem`` faults use ``tc qdisc replace`` (idempotent) rather than
   ``tc qdisc add``, so running ``start()`` on a machine that already has a
   qdisc configured does not silently no-op.


Prerequisites
-------------

.. code-block:: bash

   # On the target machine
   sudo apt-get install -y iproute2          # tc netem (usually pre-installed)

   # For SilentNetworkCorrupt only
   sudo apt-get install -y linux-headers-$(uname -r) clang llvm

All network faults require **passwordless sudo** for the ``tc`` command::

   ubuntu ALL=(ALL) NOPASSWD: /sbin/tc, /usr/sbin/tc
   # save to /etc/sudoers.d/chaos-jungle


NetworkDelay
------------

Adds a fixed delay (and optional jitter) to every outgoing packet on the
specified interface.  ``stop()`` removes the qdisc rule.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # 100 ms base delay, ±10 ms jitter
   fault = NetworkDelay("100ms", jitter="10ms")
   runner = ChaosRunner(Scenario("net-delay", [fault]), target)

   runner.start()
   # measure workload latency
   runner.stop()

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``delay``
     - required
     - Base delay: ``"100ms"``, ``"1s"``, ``"500us"``
   * - ``jitter``
     - ``""``
     - Variation around ``delay``: ``"10ms"``
   * - ``iface``
     - auto-detect
     - Network interface: ``"eth0"``, ``"ens3"``

**What to observe:**

* ``duration_s`` delta — is it close to the configured delay?
* Does the application's timeout fire within the expected window?
* Under jitter, does the retry budget handle variable RTT?

**Default metrics:** ``duration_s``, ``error_rate``, ``timeout_rate``, ``rtt_ms``, ``p50_latency_ms``, ``p99_latency_ms``


NetworkLoss
-----------

Drops a percentage of packets.  TCP retransmits recover most dropped packets,
but at the cost of latency.  High loss rates (≥ 30 %) cause TCP to stall.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkLoss, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   fault = NetworkLoss("5%")
   runner = ChaosRunner(Scenario("net-loss", [fault]), target)
   runner.start()
   runner.stop()

   # Severe loss — test circuit-breaker / fallback
   fault2 = NetworkLoss("30%")

**What to observe:**

* At 5 %: ``retries`` should increase, ``success`` stays 1 (TCP recovers)
* At 30 %: ``success`` may drop; does the circuit-breaker open?
* At 90 %: application should fall back or return a meaningful error

**Default metrics:** ``duration_s``, ``error_rate``, ``packet_loss_rate``, ``retransmissions``


NetworkCorrupt
--------------

Randomly flips bits in N % of packets.  Because tc netem recomputes the TCP
checksum, the corruption is **visible** — the kernel will reject the packet
and TCP will retransmit.  This tests application-layer retry logic, not
silent data acceptance.

.. code-block:: python

   from chaos_jungle import NetworkCorrupt

   fault = NetworkCorrupt("1%")   # 1 in 100 packets corrupted (TCP rejects it)

**What to observe:**

* ``errors`` / ``retries`` should increase
* ``success`` should stay 1 if retry logic is in place
* At high rates (10 %+): retransmission overhead causes latency spike

**Default metrics:** ``duration_s``, ``error_rate``, ``parse_errors``, ``checksum_errors``


NetworkDuplicate
----------------

Sends each affected packet twice.  Tests **idempotency** — if the application
processes the same request or response twice, the result must be unchanged.

.. code-block:: python

   from chaos_jungle import NetworkDuplicate

   fault = NetworkDuplicate("0.5%")   # 0.5 % of packets duplicated

**What to observe:**

* ``duplicate_count`` on the receiving side
* Do duplicate write operations cause data inconsistency?
* Does the application detect and deduplicate responses?

**Default metrics:** ``duration_s``, ``error_rate``, ``throughput_bps``, ``bandwidth_wasted_bytes``


SilentNetworkCorrupt
--------------------

.. code-block:: text

   NetworkCorrupt  (tc netem)           SilentNetworkCorrupt  (BPF/XDP)
   ──────────────────────────           ─────────────────────────────────
   packet leaves NIC                    packet leaves NIC
        │                                    │
        ▼                                    ▼
   [ tc netem ]                         [ BPF hook ]
    flip bit in payload                  flip bit in payload
    recalculate TCP checksum             checksum NOT updated
        │                                    │
        ▼                                    ▼
   destination receives packet          destination receives packet
   kernel sees BAD checksum             kernel sees GOOD checksum
   TCP drops + retransmits              TCP delivers to application
   ─────────────────────────            ────────────────────────────
   VISIBLE — tests retry logic          SILENT — tests integrity checks

Flips bits using a BPF / XDP hook *before* the TCP checksum is recalculated.
The kernel sees a valid packet — TCP delivers it — but the payload bytes are
wrong.  This is the most dangerous class of network fault because standard
TCP/IP layers do not detect it.

.. code-block:: python

   from chaos_jungle import SilentNetworkCorrupt, SSHTarget, Scenario, ChaosRunner

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # TC hook — modify packets leaving the interface
   fault = SilentNetworkCorrupt(rate=5000, hook="tc")

   # XDP hook — faster, processes packets at driver level
   fault2 = SilentNetworkCorrupt(rate=1000, hook="xdp")

   # Target a specific link by IP
   fault3 = SilentNetworkCorrupt(rate=2000, link_ip="10.100.1.2")

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``rate``
     - required
     - Corrupt one byte every N bytes (lower = more corruption)
   * - ``hook``
     - ``"tc"``
     - BPF attachment point: ``"tc"`` or ``"xdp"``
   * - ``link_ip``
     - ``""``
     - Only corrupt traffic to/from this IP

**What to observe:**

* ``integrity_ok`` — does the application validate payload checksums?
* Silent corruption passes TCP but fails application-layer SHA-256 / HMAC checks
* This fault exposes missing app-layer integrity validation — the most common
  finding in storage and ML pipelines

.. warning::

   ``SilentNetworkCorrupt`` requires kernel BPF support (Linux 4.15+) and
   either clang/LLVM for XDP or the ``tc`` BPF subsystem.


NetworkBandwidthLimit
---------------------

Caps the outgoing throughput of the interface using ``tc netem rate``.
Use this to simulate constrained WAN links or bandwidth-throttled cloud
egress.

.. code-block:: python

   from chaos_jungle import NetworkBandwidthLimit

   fault = NetworkBandwidthLimit("1mbit")          # 1 Mbit/s cap
   fault = NetworkBandwidthLimit("500kbit", iface="eth0")

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``rate``
     - required
     - Bandwidth ceiling: ``"1mbit"``, ``"500kbit"``, ``"10mbit"``
   * - ``iface``
     - auto-detect
     - Network interface: ``"eth0"``, ``"ens3"``

**What to observe:**

* Does the agent respect an overall token budget when streaming slows down?
* Does throughput-dependent inference (large context windows) time out correctly?

**Default metrics:** ``throughput_bps``, ``duration_s``, ``error_rate``, ``timeout_rate``


NetworkReorder
--------------

Delivers a percentage of packets out of order using ``tc netem reorder``.
A base ``delay`` is required — ``tc netem`` cannot reorder without first
introducing some queue depth.

.. code-block:: python

   from chaos_jungle import NetworkReorder

   fault = NetworkReorder("25%")                   # 25 % of packets reordered
   fault = NetworkReorder("50%", delay="100ms")    # with custom queue delay

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``rate``
     - required
     - Percentage of packets to reorder: ``"25%"``, ``"50%"``
   * - ``delay``
     - ``"50ms"``
     - Queue depth delay required to enable reordering
   * - ``iface``
     - auto-detect
     - Network interface

**What to observe:**

* Does TCP reassembly recover silently (at the cost of latency)?
* Do application-layer sequence numbers or streaming SSE events arrive in order?

**Default metrics:** ``duration_s``, ``error_rate``, ``rtt_ms``, ``packet_reorder_rate``


NetworkReset
------------

Injects TCP RST packets via ``iptables REJECT --reject-with tcp-reset``.
Affected connections are immediately terminated as if the remote host
crashed mid-session.

Each fault instance creates a uniquely tagged ``iptables`` rule so
multiple concurrent experiments do not interfere with each other.
``stop()`` removes exactly the rule created by this instance.

.. code-block:: python

   from chaos_jungle import NetworkReset

   fault = NetworkReset()                   # RST all outgoing TCP
   fault = NetworkReset(dport=443)          # RST outgoing HTTPS only
   fault = NetworkReset(direction="INPUT")  # RST incoming connections

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``dport``
     - ``0`` (all ports)
     - Destination TCP port to target
   * - ``sport``
     - ``0`` (all ports)
     - Source TCP port to target
   * - ``direction``
     - ``"OUTPUT"``
     - iptables chain: ``"OUTPUT"``, ``"INPUT"``, ``"FORWARD"``

**What to observe:**

* Does the SDK raise ``ConnectionResetError`` / ``httpx.RemoteProtocolError``?
* Does the agent retry after a RST or propagate the error to the caller?

**Default metrics:** ``error_rate``, ``connection_errors``, ``retry_count``, ``duration_s``

.. warning::

   ``NetworkReset`` requires ``iptables`` and ``sudo`` on the target machine.


NetworkPartition
----------------

Drops all traffic to (and optionally from) a specific IP address using
``iptables DROP``.  Simulates a network partition between two services.

Like ``NetworkReset``, each instance uses a unique rule comment so
``stop()`` removes only its own rule.

.. code-block:: python

   from chaos_jungle import NetworkPartition

   fault = NetworkPartition("10.0.0.2")                        # block one node
   fault = NetworkPartition("10.0.0.2", block_input=False)     # outbound only

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``dest_ip``
     - required
     - IP address to block
   * - ``block_input``
     - ``True``
     - Also block inbound traffic from ``dest_ip``

**What to observe:**

* Does the distributed system detect the split-brain within its expected timeout?
* Does the application fall back to a replica or return a degraded response?
* Is there any asymmetric behaviour when only ``block_input=False``?

**Default metrics:** ``error_rate``, ``connection_errors``, ``duration_s``, ``timeout_rate``

.. warning::

   ``NetworkPartition`` requires ``iptables`` and ``sudo`` on the target machine.


Combined network scenarios
--------------------------

Combine faults to simulate realistic degraded links:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, NetworkLoss, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Realistic WAN link — 200 ms + 2 % loss
   scenario = Scenario("wan-degraded", [
       NetworkDelay("200ms", jitter="20ms"),
       NetworkLoss("2%"),
   ])
   runner = ChaosRunner(scenario, target)
   runner.start()
   runner.stop()

   # Slow + silent corruption — bad NIC simulation
   from chaos_jungle import SilentNetworkCorrupt
   scenario2 = Scenario("bad-nic", [
       NetworkDelay("50ms"),
       SilentNetworkCorrupt(rate=3000, hook="tc"),
   ])


Measuring impact with ``runner.measure()``
-------------------------------------------

.. code-block:: python

   import time, requests
   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, SSHTarget

   target  = SSHTarget("10.0.0.5", user="ubuntu")
   runner  = ChaosRunner(
       Scenario("delay-measure", [NetworkDelay("200ms")]),
       target,
   )

   def workload():
       t0 = time.time()
       r  = requests.get("http://10.0.0.5:8080/api/ping", timeout=5.0)
       return {
           "duration_s": round(time.time() - t0, 2),
           "success":    int(r.status_code == 200),
       }

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())
   # fault_mean("duration_s") should be ≈ baseline_mean + 0.2 s

Interface auto-detection
------------------------

If ``iface`` is not specified, the fault is applied to **all non-loopback
UP interfaces** returned by ``ip -o link show up``.  This covers most
single-NIC machines automatically.

To target a specific interface::

   NetworkDelay("100ms", iface="eth0")
   NetworkDelay("100ms", iface="ens3")

See also
--------

* :ref:`guide-process` — process/service/container faults
* :ref:`guide-resources` — CPU / memory / disk exhaustion
* :ref:`guide-ssh` — SSHTarget setup and passwordless sudo
