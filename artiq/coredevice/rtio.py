from artiq.language.core import syscall
from artiq.language.types import TInt64, TInt32, TNone


@syscall
def rtio_output(time_mu: TInt64, channel: TInt32, addr: TInt32, data: TInt32
                ) -> TNone:
    raise NotImplementedError("syscall not simulated")


@syscall
def rtio_input_timestamp(timeout_mu: TInt64, channel: TInt32) -> TInt64:
    raise NotImplementedError("syscall not simulated")
