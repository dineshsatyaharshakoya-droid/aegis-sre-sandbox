interface PatchTerminalProps {
  filename: string | undefined;
  diffText: string | undefined;
}

export const PatchTerminal = ({ filename, diffText }: PatchTerminalProps) => {
  const renderDiff = (text: string) => {
    return text.split('\n').map((line, idx) => {
      let className = "diff-line";
      if (line.startsWith('+')) className += " diff-add";
      else if (line.startsWith('-')) className += " diff-sub";
      else if (line.startsWith('@@')) className += " diff-meta";
      
      return <div key={idx} className={className}>{line || ' '}</div>;
    });
  };

  return (
    <div className="terminal-window">
      <div className="terminal-header">
        <span className="terminal-title">{filename || 'Unknown File'}</span>
      </div>
      <div className="terminal-body">
        {diffText ? renderDiff(diffText) : 'Loading diff...'}
      </div>
    </div>
  );
};
