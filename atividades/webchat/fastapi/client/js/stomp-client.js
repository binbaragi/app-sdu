function connectStomp(username) {
    const wsUrl = `ws://${RABBITMQ_HOST}:${RABBITMQ_WS_PORT}/ws`;
    
    stompClient = new StompJs.Client({
        brokerURL: wsUrl,
        connectHeaders: {
            login: 'guest',
            passcode: 'guest',
        },
        debug: function(str) {
            log("[STOMP] " + str);
        },
        reconnectDelay: 5000,
        heartbeatIncoming: 4000,
        heartbeatOutgoing: 4000,
    });

    stompClient.onConnect = function() {
        // Subscribe to direct messages
        stompClient.subscribe(`/queue/user_${username}`, function(message) {
            const msg = JSON.parse(message.body);
            // Debug: log incoming queued messages
            console.debug('[STOMP] queue message:', msg, 'headers=', message.headers);
            // If the broker stored/forwarded the message without a `to` field,
            // we know this came from the user's dedicated queue, so treat it as a DM.
            if (!msg.to) msg.to = username;
            // Ensure sender exists (some messages might be missing sender)
            if (!msg.sender) msg.sender = msg.from || msg.from_user || msg.origin || message.headers && (message.headers['sender'] || message.headers['username']) || 'unknown';
            handleMessage(msg);
        });

        // Subscribe to broadcast
        stompClient.subscribe('/exchange/chat_broadcast', function(message) {
            const msg = JSON.parse(message.body);
            handleMessage(msg);
        });

        log("[STOMP] Connected");
    };

    stompClient.onStompError = function(frame) {
        log('[STOMP] Error: ' + frame.headers['message']);
        log('[STOMP] Additional details: ' + frame.body);
    };

    stompClient.activate();
}

function disconnectStomp() {
    if (stompClient) {
        stompClient.deactivate();
        stompClient = null;
    }
}

function sendMessage(msg) {
    const activeTab = document.querySelector(".tab.active").dataset.target;
    const payload = {
        type: "message",
        text: msg,
        sender: $("#username").val(),
        sent_at: new Date().toISOString()
    };

    if (activeTab === "broadcast") {
        // Send to broadcast exchange
        stompClient.publish({
            destination: '/exchange/chat_broadcast',
            body: JSON.stringify(payload)
        });
    } else {
        // Send to user's queue (direct message) - include `to` so receivers know it's a DM
        payload.to = activeTab;
        stompClient.publish({
            destination: `/queue/user_${activeTab}`,
            body: JSON.stringify(payload)
        });
        // Show in sender's window too
        appendMessage(activeTab, payload);
    }
}

function handleMessage(msg) {
    if (msg.type === "message") {
        if (msg.to) {
            ensureTab(msg.sender);
            appendMessage(msg.sender, msg);
        } else {
            appendMessage("broadcast", msg);
        }
    }
}