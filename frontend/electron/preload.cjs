const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktop', {
  getScreenState: () => ipcRenderer.invoke('desktop:get-screen-state'),
  selectScreenDisplay: (displayId) => ipcRenderer.invoke('desktop:select-screen-display', displayId),
  openScreen: () => ipcRenderer.invoke('desktop:open-screen'),
  moveScreen: () => ipcRenderer.invoke('desktop:move-screen'),
  closeScreen: () => ipcRenderer.invoke('desktop:close-screen'),
  openConfiguration: () => ipcRenderer.invoke('desktop:open-configuration'),
  toggleScreenFullscreen: () => ipcRenderer.invoke('desktop:toggle-screen-fullscreen'),
  getRuntime: () => ipcRenderer.invoke('desktop:runtime'),
  onScreenStateChanged: (listener) => {
    const wrapped = (_, state) => listener(state);
    ipcRenderer.on('desktop:screen-state-changed', wrapped);
    return () => ipcRenderer.removeListener('desktop:screen-state-changed', wrapped);
  },
});
