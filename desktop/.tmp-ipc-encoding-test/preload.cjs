const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('testApi', { send() { ipcRenderer.send('test:msg', { message: '模型已加载' }); } });
