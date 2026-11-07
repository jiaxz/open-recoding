"""One SSH connection bridges the private same-user runtime socket."""
import json,socket,sys,threading


def main():
    auth=json.loads(sys.stdin.buffer.readline(65536));address=auth.pop('address')
    channel=socket.socket(socket.AF_UNIX);channel.connect('\0'+address)
    channel.sendall(json.dumps(auth).encode()+b'\n')
    def incoming():
        try:
            while True:
                data=channel.recv(65536)
                if not data:break
                sys.stdout.buffer.write(data);sys.stdout.buffer.flush()
        finally:
            try:channel.shutdown(socket.SHUT_RDWR)
            except OSError:pass
    thread=threading.Thread(target=incoming,daemon=True);thread.start()
    try:
        while thread.is_alive():
            data=sys.stdin.buffer.read1(65536)
            if not data:break
            channel.sendall(data)
    finally:
        try:channel.shutdown(socket.SHUT_RDWR)
        except OSError:pass
        channel.close();thread.join(timeout=2)
