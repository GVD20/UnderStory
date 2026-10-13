import os
import socket
import uvicorn

if __name__ == '__main__':
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    from understory_api.main import app
    print(f'UNDERSTORY_READY:{listener.getsockname()[1]}', flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level='warning')).run(sockets=[listener])
