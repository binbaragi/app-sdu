import asyncio
import websockets

clientes = set()

async def echo(websocket):
    clientes.add(websocket)
    try:
        print(f"Novo cliente conectado: {websocket.remote_address}")
        print(f"Total de clientes conectados: {len(clientes)}")
        
        async for message in websocket:
            print(f"Mensagem recebida de {websocket.remote_address}: {message}")
            for client in clientes:
                if client != websocket:
                    await client.send(f"{websocket.remote_address}: {message}")
    except websockets.exceptions.ConnectionClosed:
        print(f"Cliente {websocket.remote_address} desconectou")
    finally:
        clientes.remove(websocket)
        print(f"Total de clientes conectados: {len(clientes)}")

async def main():
    async with websockets.serve(echo, "localhost", 8765):
        print("Servidor WebSocket rodando em ws://localhost:8765")
        await asyncio.Future() 

if __name__ == "__main__":
    asyncio.run(main())
    
