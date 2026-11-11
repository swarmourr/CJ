Quickstart
==========

This example verifies a same-process HTTP delay without an API key, a model,
or a privileged target. Install and verify CJ first with
:doc:`guides/installation`.

Run a bounded fault
-------------------

``inject(...)`` temporarily intercepts supported ``httpx`` and ``requests``
clients. The mock transport below keeps the demonstration local while still
exercising the HTTP interception path.

.. code-block:: python

   import time
   import httpx

   from chaos_jungle.inject.intercept import Latency, inject

   client = httpx.Client(
       transport=httpx.MockTransport(
           lambda request: httpx.Response(200, json={"ok": True})
       )
   )

   def workload():
       start = time.perf_counter()
       response = client.get("https://example.invalid/v1/check")
       return response.status_code, time.perf_counter() - start

   control_status, control_seconds = workload()
   with inject(Latency(0.05), urls=["example.invalid"]):
       fault_status, fault_seconds = workload()
   client.close()

   print(f"control: {control_status}, {control_seconds:.3f} s")
   print(f"fault:   {fault_status}, {fault_seconds:.3f} s")

Both calls should return 200; the faulted call should take roughly 50 ms
longer. The context manager removes the interception on exit, including when
the block raises an exception. The URL filter means other HTTP destinations
are not selected by this example.

Apply it to an application
--------------------------

Replace ``workload()`` with a real agent or service call made in the same
Python process. Check that its HTTP client uses ``httpx`` or ``requests`` and
that its destination matches the selected URL. A configured fault does not
prove the call was reached.

For a model client running in another process or container, choose a managed
proxy or an explicit target instead; see :ref:`guide-choosing-injection`.
For a fault that must remain active around a function or a longer-running
workload, use :doc:`guides/choosing-injection` to choose between
``@chaos``, ``chaos_session(...)``, and ``ChaosRunner``.

Interpret the result
--------------------

The response status and timing above are application observations chosen for
this example. In a real experiment, specify the workload's success condition
separately from evidence that the fault was activated, reached, and manifested.
CJ's session record and exposure verdict can help distinguish a genuine
fault response from an operation that was never called. Cleanup and
post-checks answer a separate question: whether the affected environment was
restored. Continue with :doc:`concepts`, :doc:`guides/measurement`, and
:doc:`guides/traces`.
