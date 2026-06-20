alias lsh='ls -la | grep " \..*"' # ls all hidden files only with grep
alias v="vi"
alias vi="vim"
alias myip='dig +short myip.opendns.com @resolver1.opendns.com'
alias gdh="git diff HEAD"
alias gdhh="git diff HEAD^ HEAD"

# Locate Homebrew (without spawning `brew` on every startup) so we can load
# the brew-installed zsh autocomplete plugins below.
if [ -x /opt/homebrew/bin/brew ]; then
    BREW_PREFIX=/opt/homebrew
elif [ -x /usr/local/bin/brew ]; then
    BREW_PREFIX=/usr/local
fi

# zsh-completions adds extra completion definitions. It must be on fpath
# *before* compinit runs (oh-my-zsh runs compinit when loaded below).
if [ -n "$BREW_PREFIX" ] && [ -d "$BREW_PREFIX/share/zsh-completions" ]; then
    fpath=("$BREW_PREFIX/share/zsh-completions" $fpath)
fi

# Homebrew formula completions (gh, brew, etc.) — also before compinit.
if [ -n "$BREW_PREFIX" ] && [ -d "$BREW_PREFIX/share/zsh/site-functions" ]; then
    fpath=("$BREW_PREFIX/share/zsh/site-functions" $fpath)
fi

# Git integration, prompt, and dev-tool plugins via oh-my-zsh.
# setup.sh installs oh-my-zsh into ~/.zsh/oh-my-zsh.
ZSH="$HOME/.zsh/oh-my-zsh"
if [ -d "$ZSH" ]; then
    export ZSH
    # Prompt theme. robbyrussell ships with oh-my-zsh and shows the current
    # git branch plus a dirty/clean indicator without needing powerline fonts.
    ZSH_THEME="robbyrussell"
    # oh-my-zsh plugins add tab-completion and handy aliases for these tools.
    # Each plugin guards on its binary being present (or is alias-only), so
    # listing a tool you haven't installed yet is harmless.
    plugins=(
        # Version control (also adds branch info to the prompt)
        git
        # Containers / orchestration
        docker
        docker-compose
        kubectl
        helm
        # Cloud / infra
        terraform
        aws
        gcloud
        # JavaScript / Node
        npm
        node
        # Python
        pip
        python
        # Other runtimes
        deno
        rust
        # Shell tooling
        fzf
        tmux
        brew
    )
    source "$ZSH/oh-my-zsh.sh"
else
    # Fallback when oh-my-zsh isn't installed: build a git-aware prompt with vcs_info.
    autoload -Uz vcs_info
    precmd() { vcs_info }
    zstyle ':vcs_info:git:*' formats ' (%b)'
    zstyle ':vcs_info:*' enable git
    setopt prompt_subst
    PROMPT='%F{cyan}%~%f%F{yellow}${vcs_info_msg_0_}%f %# '
fi

# Optional user overrides — create ~/.zshrc.local yourself (not managed by this repo).
ZSHRC_LOCAL="$HOME/.zshrc.local"
if [ -e "$ZSHRC_LOCAL" ]; then
    source $ZSHRC_LOCAL
fi

# {{{ Autocomplete / completion
# Initialise the completion system (oh-my-zsh already runs compinit when loaded).
if ! whence compdef >/dev/null 2>&1; then
    autoload -Uz compinit && compinit
fi

# Some dev CLIs ship their own zsh completion generator instead of a static
# completion file, and aren't covered by an oh-my-zsh plugin. Generating the
# script on every startup spawns a subprocess and is slow, so cache the output
# and only regenerate when the tool's binary is newer than the cache (e.g.
# after an upgrade). These run after compinit so they win over any earlier
# registration.
ZSH_COMPLETION_CACHE="$HOME/.zsh/completion-cache"
[ -d "$ZSH_COMPLETION_CACHE" ] || mkdir -p "$ZSH_COMPLETION_CACHE"

# load_completion <cache-name> <command> [args...]
#   <command> [args...] must print a zsh completion script to stdout.
load_completion() {
    local name="$1"; shift
    local bin="$1"
    command -v "$bin" >/dev/null 2>&1 || return
    local cache="$ZSH_COMPLETION_CACHE/$name.zsh"
    if [ ! -s "$cache" ] || [ "$(command -v "$bin")" -nt "$cache" ]; then
        "$@" >| "$cache" 2>/dev/null || { rm -f "$cache"; return; }
    fi
    source "$cache"
}

load_completion gh       gh completion -s zsh
load_completion supabase supabase completion zsh

# Case-insensitive, partial-word and substring matching while completing.
zstyle ':completion:*' matcher-list 'm:{a-zA-Z}={A-Za-z}' 'r:|=*' 'l:|=* r:|=*'
# Navigate completion candidates with the arrow keys.
zstyle ':completion:*' menu select
# Group results and give each group a coloured header.
zstyle ':completion:*' group-name ''
zstyle ':completion:*:descriptions' format '%F{yellow}%d%f'
# Cache slow completions (e.g. brew) for faster subsequent use.
zstyle ':completion:*' use-cache on
zstyle ':completion:*' cache-path "$HOME/.zsh/cache"
zstyle ':completion:*' list-colors ''

# Fish-like inline autosuggestions from history (brew install zsh-autosuggestions).
if [ -n "$BREW_PREFIX" ] && [ -f "$BREW_PREFIX/share/zsh-autosuggestions/zsh-autosuggestions.zsh" ]; then
    source "$BREW_PREFIX/share/zsh-autosuggestions/zsh-autosuggestions.zsh"
fi

# History-driven completion: type a prefix then Up/Down to search history.
HISTFILE="$HOME/.zsh_history"
HISTSIZE=50000
SAVEHIST=50000
setopt share_history inc_append_history hist_ignore_dups hist_ignore_space
autoload -Uz up-line-or-beginning-search down-line-or-beginning-search
zle -N up-line-or-beginning-search
zle -N down-line-or-beginning-search
bindkey '^[[A' up-line-or-beginning-search
bindkey '^[[B' down-line-or-beginning-search

# Syntax highlighting must be sourced last (brew install zsh-syntax-highlighting).
if [ -n "$BREW_PREFIX" ] && [ -f "$BREW_PREFIX/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh" ]; then
    source "$BREW_PREFIX/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
fi
# }}}
