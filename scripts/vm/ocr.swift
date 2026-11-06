// Runs on the host: finds text in a screenshot of the test VM.
//   ocr <png> <text>   prints the centre of the first line containing <text>
//                      (case-insensitive) in image pixels, "x y"; exits 1 if none.
//                      "=<text>" matches a line that is exactly <text>.
//   ocr <png>          prints every line it reads
import AppKit
import Vision

let args = CommandLine.arguments
guard args.count >= 2, let image = NSImage(contentsOfFile: args[1]),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write(Data("usage: ocr <png> [text]\n".utf8))
    exit(2)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
try VNImageRequestHandler(cgImage: cg).perform([request])
let lines = (request.results ?? []).compactMap { obs -> (String, CGRect)? in
    guard let text = obs.topCandidates(1).first?.string else { return nil }
    return (text, obs.boundingBox)
}
guard args.count >= 3 else {
    for (text, _) in lines { print(text) }
    exit(0)
}
let exact = args[2].hasPrefix("=")
let wanted = (exact ? String(args[2].dropFirst()) : args[2]).lowercased()
guard let (_, box) = lines.first(where: {
    let line = $0.0.lowercased().trimmingCharacters(in: .whitespaces)
    return exact ? line == wanted : line.contains(wanted)
}) else { exit(1) }
// Vision's boxes are normalised with the origin at the bottom left.
print(Int(box.midX * CGFloat(cg.width)), Int((1 - box.midY) * CGFloat(cg.height)))
