Exposure API
============

Activation and cleanup status do not prove that a workload encountered a
fault. Use these records to classify observed triggers and effects separately
from application outcomes. See :ref:`guide-measurement` for the session API.

.. automodule:: chaos_jungle.exposure
   :members: ExposureVerdict, classify_fault_exposure, aggregate_exposure
