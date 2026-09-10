if (window.location.pathname.startsWith('/tasks') || window.location.pathname === '/') {
  void import('./dashboard')
} else {
  void import('./main')
}
