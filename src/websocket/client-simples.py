import websocket

def on_connect(ws):
    print("Conectado ao servidor.")

def on_message(ws, message):
    print(message)  # Mostra a mensagem diretamente, já que o servidor inclui o remetente

def on_error(ws, error):
    print(f"Erro: {error}")

def on_close(ws):
    print("Conexão encerrada.")

def on_open(ws):
    print("Bem-vindo ao chat!")
    print("Digite suas mensagens (ou 'sair' para encerrar):")
    
    # Thread para enviar mensagens
    def send_messages():
        while True:
            try:
                message = input()
                if message.lower() == 'sair':
                    ws.close()
                    break
                ws.send(message)
            except Exception as e:
                print(f"Erro ao enviar mensagem: {e}")
                break
    
    import threading
    sender_thread = threading.Thread(target=send_messages)
    sender_thread.daemon = True
    sender_thread.start()

if __name__ == "__main__":
    ws = websocket.WebSocketApp("ws://localhost:8765",
                            on_message=on_message,
                            on_error=on_error,
                            on_close=on_close,
                            on_open=on_open
                        )
    ws.run_forever()
