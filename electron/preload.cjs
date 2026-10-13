const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('desktop', {
  bootstrap: () => ipcRenderer.invoke('bootstrap'),
  savePreferences: value => ipcRenderer.invoke('preferences', value),
  chooseLibrary: () => ipcRenderer.invoke('library-choose'),
  writeSecret: (name, value) => ipcRenderer.invoke('secret-write', name, value),
  readSecret: name => ipcRenderer.invoke('secret-read', name),
});
