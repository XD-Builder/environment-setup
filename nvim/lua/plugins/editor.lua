-- Small editor tweaks for neo-tree parity with NERDTree ignores.
return {
  {
    "nvim-neo-tree/neo-tree.nvim",
    opts = {
      filesystem = {
        filtered_items = {
          hide_dotfiles = false,
          hide_gitignored = true,
          hide_by_name = {
            "node_modules",
            "__pycache__",
            ".git",
          },
          hide_by_pattern = {
            "*.pyc",
            "*.o",
            "*.obj",
            "*.swp",
          },
        },
        follow_current_file = { enabled = true },
        use_libuv_file_watcher = true,
      },
      window = {
        width = 30,
      },
    },
  },
}
