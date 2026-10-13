module.exports = {
  packagerConfig: {
    name: 'UnderStory', executableName: 'UnderStory', asar: true,
    icon: 'assets/brand/app-icon.ico',
    extraResource: ['build/understory-backend', 'assets/brand/app-icon.ico', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'docs/licenses'],
    ignore: file => {
      const parts = file.split('/').filter(Boolean);
      if (!parts.length) return false;
      if (parts[0] === 'node_modules') return false;
      return !['electron', 'dist', 'package.json', 'LICENSE', 'THIRD_PARTY_NOTICES.md'].includes(parts[0]);
    },
  },
  makers: [{ name: '@electron-forge/maker-zip', platforms: ['win32'] }],
};
