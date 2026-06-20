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
# *before* compinit runs (oh-my-zsh runs compinit when .zshrc.local is sourced).
if [ -n "$BREW_PREFIX" ] && [ -d "$BREW_PREFIX/share/zsh-completions" ]; then
    fpath=("$BREW_PREFIX/share/zsh-completions" $fpath)
fi

ZSHRC_LOCAL="$HOME/.zshrc.local"
if [ -e "$ZSHRC_LOCAL" ]; then
    source $ZSHRC_LOCAL
fi

# {{{ Autocomplete / completion
# Initialise the completion system (oh-my-zsh already runs compinit when loaded).
if ! whence compdef >/dev/null 2>&1; then
    autoload -Uz compinit && compinit
fi

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
