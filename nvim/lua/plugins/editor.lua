-- Small editor tweaks for neo-tree parity with NERDTree ignores.
-- LazyVim's neo-tree extra binds <leader>e/E to explorer; disable those so
-- vim-style quit (,e / ,E) wins, and bind ,z for the tree instead.
return {
  {
    "nvim-neo-tree/neo-tree.nvim",
    keys = {
      { "<leader>e", false },
      { "<leader>E", false },
      {
        "<leader>z",
        function()
          require("neo-tree.command").execute({ toggle = true, dir = LazyVim.root() })
        end,
        desc = "Toggle file tree",
      },
    },
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

  -- `,n` notification picker: Enter focuses the preview so you can visual-select
  -- (`v`/`V` then `y`). Default confirm just closed the picker.
  {
    "folke/snacks.nvim",
    opts = {
      picker = {
        sources = {
          notifications = {
            confirm = "focus_preview",
          },
        },
      },
    },
  },
}
