function RawInline(el)
  if el.format == "html" then
    if string.match(el.text, "^%s*<br%s*/?>%s*$") or
       string.match(el.text, "^%s*<br%s+[^>]*%s*/?%s*>%s*$") then
      return pandoc.LineBreak()
    end
  end
  return nil
end
