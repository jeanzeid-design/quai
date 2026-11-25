// Quai — installe des jeux sur la ROG Ally depuis le Mac.
//
// Le Mac ne fait que trois choses : envoyer le fichier (rsync par SSH, alias « ally »
// de ~/.ssh/config, clé ~/.ssh/id_ally), poser les questions à Jean, et afficher ce que
// le moteur raconte. Tout le travail est fait sur l'Ally par quai_ally.py, embarqué dans
// l'application (Contents/Resources) et redéployé automatiquement s'il a changé.
// Protocole : une ligne JSON par événement (voir l'en-tête de quai_ally.py).

import SwiftUI
import AppKit
import CryptoKit
import UniformTypeIdentifiers

@objc(SilentApplication)
final class SilentApplication: NSApplication {
    override func noResponder(for eventSelector: Selector) { }
}

// MARK: - Accès à l'Ally

/// Découpe la sortie en lignes ; verrouillé car le lecteur et la fin du processus tournent sur deux files.
final class Tampon: @unchecked Sendable {
    private var data = Data()
    private let verrou = NSLock()
    func ajouter(_ d: Data) -> [String] {
        verrou.lock(); defer { verrou.unlock() }
        data.append(d)
        var lignes: [String] = []
        while let i = data.firstIndex(of: 0x0A) {
            lignes.append(String(decoding: data[data.startIndex..<i], as: UTF8.self))
            data.removeSubrange(data.startIndex...i)
        }
        return lignes
    }
    func vider() -> [String] {
        verrou.lock(); defer { verrou.unlock() }
        let l = data.isEmpty ? [] : [String(decoding: data, as: UTF8.self)]
        data = Data()
        return l
    }
}

enum Ally {
    static let hote = "ally"
    static let moteurDistant = ".local/share/quai/quai_ally.py"
    static let ssh = "/usr/bin/ssh"
    static let options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15",
                          "-o", "LogLevel=ERROR"]

    static func q(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

    /// Lance une commande du moteur et transmet chaque événement JSON, au fil de l'eau.
    @discardableResult
    static func commande(_ cmd: String, _ args: [String: Any] = [:],
                         evenement: @escaping ([String: Any]) -> Void) async -> Int32 {
        let json = (try? JSONSerialization.data(withJSONObject: args)).flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        let distant = "python3 -I \(moteurDistant) \(cmd) \(q(json))"
        return await lancer(ssh, options + [hote, distant]) { ligne in
            guard let d = ligne.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
            DispatchQueue.main.async { evenement(obj) }
        }
    }

    /// Variante qui rend seulement l'événement final (ou l'erreur).
    static func appel(_ cmd: String, _ args: [String: Any] = [:]) async -> [String: Any]? {
        var dernier: [String: Any]?
        await commande(cmd, args) { ev in
            if ev["t"] as? String == "fin" || ev["t"] as? String == "erreur" { dernier = ev }
        }
        try? await Task.sleep(nanoseconds: 50_000_000)   // laisser passer le dernier DispatchQueue.main.async
        return dernier
    }

    static func lancer(_ exe: String, _ args: [String], ligne: @escaping (String) -> Void) async -> Int32 {
        await withCheckedContinuation { cont in
            let p = Process()
            p.executableURL = URL(fileURLWithPath: exe)
            p.arguments = args
            let sortie = Pipe()
            p.standardOutput = sortie
            p.standardError = Pipe()
            let tampon = Tampon()
            sortie.fileHandleForReading.readabilityHandler = { h in
                let morceau = h.availableData
                if morceau.isEmpty { return }
                tampon.ajouter(morceau).forEach(ligne)
            }
            p.terminationHandler = { proc in
                sortie.fileHandleForReading.readabilityHandler = nil
                let reste = sortie.fileHandleForReading.readDataToEndOfFile()
                tampon.ajouter(reste).forEach(ligne)
                tampon.vider().forEach(ligne)
                cont.resume(returning: proc.terminationStatus)
            }
            do { try p.run() } catch { cont.resume(returning: -1) }
        }
    }

    /// Met le moteur à jour sur l'Ally s'il diffère de celui embarqué dans l'application.
    static func deployerMoteur() async -> Bool {
        guard let local = Bundle.main.url(forResource: "quai_ally", withExtension: "py"),
              let data = try? Data(contentsOf: local) else { return false }
        let empreinte = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        var distante = ""
        _ = await lancer(ssh, options + [hote, "sha256sum \(moteurDistant) 2>/dev/null | cut -d' ' -f1"]) { distante += $0 }
        if distante.trimmingCharacters(in: .whitespaces) == empreinte { return true }
        _ = await lancer(ssh, options + [hote, "mkdir -p .local/share/quai"]) { _ in }
        let code = await lancer("/usr/bin/scp", ["-q"] + options + [local.path, "\(hote):\(moteurDistant)"]) { _ in }
        return code == 0
    }

    /// Envoie un fichier ou un dossier ; `progres` reçoit 0…1 (mesuré côté Ally toutes les 2 s).
    static func envoyer(_ url: URL, vers dossier: String, taille: Int64,
                        progres: @escaping (Double) -> Void) async -> Bool {
        let sources = [url] + Quai.morceaux(url)
        let cibles = sources.map { q(dossier + "/" + $0.lastPathComponent) }.joined(separator: " ")
        let suivi = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                var t = ""
                _ = await lancer(ssh, options + [hote, "du -scb \(cibles) 2>/dev/null | tail -1 | cut -f1"]) { t += $0 }
                if let v = Double(t.trimmingCharacters(in: .whitespaces)), taille > 0 {
                    let pct = min(v / Double(taille), 1)
                    await MainActor.run { progres(pct) }
                }
            }
        }
        let code = await lancer("/usr/bin/rsync", ["-a", "--partial", "-e", "ssh " + options.joined(separator: " "),
                                                    ] + sources.map(\.path) + ["\(hote):\(dossier)/"]) { _ in }
        suivi.cancel()
        return code == 0
    }
}

// MARK: - Modèle

struct Jeu: Identifiable, Hashable {
    let id: String, nom: String, type: String, dest: String, etat: String, taille: Int64, cree: String
    var enCours = false, relancable = false
}

struct Candidat: Identifiable, Hashable {
    var id: String { chemin }
    let chemin: String, nom: String, taille: Int64
}

enum Etape: Equatable {
    case aucune
    case destination(URL, Int64)                  // choisir interne / microSD
    case envoi(String, Double)                    // nom du fichier, 0…1
    case confirmation                             // nom + type, avant d'installer
    case travail                                  // le moteur travaille
    case choixExe(String, [Candidat])             // id du jeu
    case interactif(String, String)               // id, message
    case fini(String)
    case erreur(String)
}

@MainActor
final class Quai: ObservableObject {
    @Published var connecte: Bool? = nil
    @Published var interne: (libre: Int64, total: Int64) = (0, 0)
    @Published var sd: (libre: Int64, total: Int64)? = nil
    @Published var session = ""
    @Published var sgdb = false
    @Published var jeux: [Jeu] = []
    @Published var etape: Etape = .aucune
    @Published var journal: [String] = []
    @Published var pct: Double? = nil
    // fichier en cours
    @Published var nom = ""
    @Published var typeDetecte = ""
    @Published var note = ""
    @Published var modeExe = "installeur"         // installeur | interactif | jeu
    @Published var forcerClics = false
    var fichierDistant = ""
    var dest = "interne"

    static let refus: [String: String] = [
        "dmg": "Un .dmg est une application Mac : elle ne peut pas tourner sur l'Ally, ni sous Linux ni avec Proton.",
        "pkg": "Un .pkg est un installeur Mac : il ne peut pas tourner sur l'Ally.",
        "app": "Un .app est une application Mac : elle ne peut pas tourner sur l'Ally.",
        "ipa": "Un .ipa est une application iPhone : elle ne peut pas tourner sur l'Ally.",
        "apk": "Un .apk est une application Android : Bazzite ne fait pas tourner Android.",
        "rpm": "Un .rpm est un paquet système : Bazzite n'en installe pas. Cherche la version Flatpak ou AppImage.",
        "deb": "Un .deb est un paquet Debian : Bazzite ne l'installe pas. Cherche la version Flatpak ou AppImage.",
    ]

    func rafraichir() async {
        let ok = await Ally.deployerMoteur()
        guard ok, let e = await Ally.appel("etat"), e["t"] as? String == "fin" else { connecte = false; return }
        connecte = true
        if let i = e["interne"] as? [String: Any] { interne = (int(i["libre"]), int(i["total"])) }
        if let s = e["sd"] as? [String: Any] { sd = (int(s["libre"]), int(s["total"])) } else { sd = nil }
        session = (e["session"] as? String) ?? ""
        sgdb = (e["sgdb"] as? Bool) ?? false
        if let l = await Ally.appel("liste"), let arr = l["jeux"] as? [[String: Any]] {
            jeux = arr.map { Jeu(id: $0["id"] as? String ?? "", nom: $0["nom"] as? String ?? "",
                                 type: $0["type"] as? String ?? "", dest: $0["dest"] as? String ?? "",
                                 etat: $0["etat"] as? String ?? "", taille: int($0["taille"]),
                                 cree: $0["cree"] as? String ?? "",
                                 enCours: $0["en_cours"] as? Bool ?? false,
                                 relancable: $0["relancable"] as? Bool ?? false) }
        }
    }

    func int(_ v: Any?) -> Int64 { (v as? NSNumber)?.int64Value ?? 0 }

    // 1. dépôt d'un fichier sur la fenêtre
    func deposer(_ url: URL) {
        guard etapeLibre else { return }
        let ext = url.pathExtension.lowercased()
        if let r = Quai.refus[ext] { etape = .erreur(r); return }
        etape = .destination(url, Quai.taille(url))
    }

    var etapeLibre: Bool {
        switch etape { case .aucune, .fini, .erreur: return true; default: return false }
    }

    /// Installeur en plusieurs morceaux (convention GOG : setup_jeu.exe + setup_jeu-1.bin, -2.bin…) :
    /// les .bin posés à côté de l'exe et qui portent son nom partent avec lui.
    nonisolated static func morceaux(_ url: URL) -> [URL] {
        guard url.pathExtension.lowercased() == "exe" else { return [] }
        let stem = url.deletingPathExtension().lastPathComponent.lowercased()
        let voisins = (try? FileManager.default.contentsOfDirectory(at: url.deletingLastPathComponent(),
                                                                    includingPropertiesForKeys: nil)) ?? []
        return voisins.filter {
            $0.pathExtension.lowercased() == "bin" && $0.lastPathComponent.lowercased().hasPrefix(stem)
        }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    static func taille(_ url: URL) -> Int64 {
        if !morceaux(url).isEmpty {
            return ([url] + morceaux(url)).reduce(0) { $0 + taille1($1) }
        }
        return taille1(url)
    }

    static func taille1(_ url: URL) -> Int64 {
        var dossier: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &dossier) else { return 0 }
        if !dossier.boolValue {
            return ((try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? NSNumber)?.int64Value ?? 0
        }
        var total: Int64 = 0
        if let en = FileManager.default.enumerator(at: url, includingPropertiesForKeys: [.fileSizeKey]) {
            for case let f as URL in en {
                total += Int64((try? f.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
            }
        }
        return total
    }

    // 2. envoi vers le dépôt choisi, puis analyse
    func envoyer(_ url: URL, dest: String) async {
        self.dest = dest
        journal = []
        let taille = Quai.taille(url)
        let libre = dest == "sd" ? (sd?.libre ?? 0) : interne.libre
        if taille * 2 > libre {
            etape = .erreur("Pas assez de place sur \(dest == "sd" ? "la microSD" : "le stockage interne") : il faut environ "
                            + "\(Quai.go(taille * 2)) (le fichier, puis le jeu installé) et il reste \(Quai.go(libre)).")
            return
        }
        guard let d = await Ally.appel("depot", ["dest": dest]), let dossier = d["chemin"] as? String else {
            etape = .erreur("L'Ally ne répond pas. Vérifie qu'elle est allumée et sur le Wi-Fi."); return
        }
        etape = .envoi(url.lastPathComponent, 0)
        let ok = await Ally.envoyer(url, vers: dossier, taille: taille) { [weak self] p in
            self?.etape = .envoi(url.lastPathComponent, p)
        }
        guard ok else { etape = .erreur("L'envoi vers l'Ally a échoué (Wi-Fi coupé ?). Redépose le fichier pour reprendre."); return }
        fichierDistant = dossier + "/" + url.lastPathComponent
        guard let a = await Ally.appel("analyser", ["fichier": fichierDistant]) else {
            etape = .erreur("L'Ally n'a pas pu analyser le fichier."); return
        }
        if a["t"] as? String == "erreur" { etape = .erreur(a["msg"] as? String ?? "Erreur"); return }
        typeDetecte = a["type"] as? String ?? ""
        note = a["note"] as? String ?? ""
        nom = a["nom"] as? String ?? url.deletingPathExtension().lastPathComponent
        modeExe = "installeur"; forcerClics = false
        if typeDetecte == "refus" {
            await effacerDepot()
            etape = .erreur(note); return
        }
        etape = .confirmation
    }

    func effacerDepot() async {
        if !fichierDistant.isEmpty { _ = await Ally.appel("effacer_depot", ["fichier": fichierDistant]) }
        fichierDistant = ""
    }

    func abandonner() async {
        await effacerDepot()
        etape = .aucune
    }

    // 3. installation
    func installer() async {
        var args: [String: Any] = ["fichier": fichierDistant, "nom": nom, "dest": dest]
        if typeDetecte == "exe" {
            if modeExe == "jeu" { args["mode"] = "jeu" } else { args["interactif"] = true }
        } else if forcerClics { args["interactif"] = true }
        // Installeur Windows automatique : le moteur passe l'Ally en mode Bureau (25/11/2026).
        if args["interactif"] == nil && args["mode"] == nil
            && ["inno", "nsis", "msi", "archive", "iso", "dossier"].contains(typeDetecte) {
            guard let b = await autoriserBureau() else { return }
            args["bureau"] = b
        }
        await suivre("installer", args)
    }

    /// Passer en mode Bureau ferme Steam, et donc un jeu lancé : jamais sans l'accord de Jean.
    /// Rend "oui" (aucun jeu ne tourne), "forcer" (Jean accepte de fermer le jeu),
    /// "non" (rester en mode jeu), ou nil (annuler).
    func autoriserBureau() async -> String? {
        guard let e = await Ally.appel("etat"), e["t"] as? String == "fin" else { return "oui" }
        guard e["session"] as? String == "jeu", let jeu = e["jeu_lance"] as? String else { return "oui" }
        let a = NSAlert()
        a.messageText = "« \(jeu) » tourne sur l'Ally"
        a.informativeText = "Pour que les fenêtres de l'installeur soient visibles, Quai passe l'Ally en mode Bureau, "
            + "puis la remet en mode jeu à la fin. Ce passage ferme Steam, donc aussi « \(jeu) » : "
            + "sauvegarde ta partie avant."
        a.addButton(withTitle: "Fermer le jeu et passer en mode Bureau")
        a.addButton(withTitle: "Installer en restant en mode jeu")
        a.addButton(withTitle: "Annuler")
        switch a.runModal() {
        case .alertFirstButtonReturn: return "forcer"
        case .alertSecondButtonReturn: return "non"
        default: return nil
        }
    }

    func choisir(_ id: String, _ c: Candidat) async { await suivre("finaliser", ["id": id, "exe": c.chemin]) }
    func interactifTermine(_ id: String) async { await suivre("reprendre", ["id": id]) }

    private func suivre(_ cmd: String, _ args: [String: Any]) async {
        etape = .travail; pct = nil
        var fin = false
        await Ally.commande(cmd, args) { [weak self] ev in
            guard let self else { return }
            switch ev["t"] as? String {
            case "etape": self.journal.append(ev["msg"] as? String ?? ""); self.pct = nil
            case "progres":
                if let p = ev["pct"] as? NSNumber { self.pct = p.doubleValue / 100 }
                if let m = ev["msg"] as? String {
                    if let last = self.journal.last, last.hasPrefix(String(m.prefix(12))) { self.journal[self.journal.count - 1] = m }
                    else { self.journal.append(m) }
                }
            case "question":
                fin = true
                let id = ev["id"] as? String ?? ""
                if ev["type"] as? String == "choisir_exe" {
                    let cs = (ev["candidats"] as? [[String: Any]] ?? []).map {
                        Candidat(chemin: $0["chemin"] as? String ?? "", nom: $0["nom"] as? String ?? "",
                                 taille: self.int($0["taille"])) }
                    self.etape = .choixExe(id, cs)
                } else {
                    self.etape = .interactif(id, ev["msg"] as? String ?? "")
                }
            case "fin":
                fin = true
                self.etape = .fini(ev["msg"] as? String ?? "Terminé.")
            case "erreur":
                fin = true
                self.etape = .erreur(ev["msg"] as? String ?? "Erreur sur l'Ally.")
            default: break
            }
        }
        try? await Task.sleep(nanoseconds: 100_000_000)
        if !fin { etape = .erreur("La connexion avec l'Ally s'est coupée pendant l'opération. Rafraîchis la liste pour voir où elle en est.") }
        fichierDistant = ""
        await rafraichir()
    }

    func desinstaller(_ j: Jeu) async {
        journal = []
        await suivre("desinstaller", ["id": j.id])
    }

    // Installation inachevée : le moteur nettoie jeu/ et pfx/ et relance
    // l'installeur resté sur l'Ally, sans renvoyer le fichier (25/11/2026).
    func relancer(_ j: Jeu) async {
        journal = []
        guard let b = await autoriserBureau() else { return }
        await suivre("relancer", ["id": j.id, "bureau": b])
    }

    func enregistrerCle(_ cle: String) async {
        _ = await Ally.appel("cle", ["cle": cle])
        await rafraichir()
    }

    static func go(_ o: Int64) -> String {
        let g = Double(o) / 1_000_000_000
        return g >= 10 ? "\(Int(g.rounded())) Go" : String(format: "%.1f Go", g).replacingOccurrences(of: ".", with: ",")
    }
}

// MARK: - Interface

let fond = Color(nsColor: .windowBackgroundColor)

struct Fenetre: View {
    @StateObject var quai = Quai()
    @State var survol = false
    @State var aDesinstaller: Jeu?
    @State var reglages = false

    var body: some View {
        VStack(spacing: 0) {
            enTete
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    zoneDepot
                    bibliotheque
                }
                .padding(20)
            }
            .scrollContentBackground(.hidden)
            .background(fond)
        }
        .background(fond)
        .frame(minWidth: 620, minHeight: 560)
        .task { await quai.rafraichir() }
        .sheet(isPresented: $reglages) { Reglages(quai: quai) }
        .alert(item: $aDesinstaller) { j in
            Alert(title: Text("Désinstaller « \(j.nom) » ?"),
                  message: Text("Le jeu est supprimé de l'Ally et retiré de Steam. Ses sauvegardes sont d'abord mises de côté dans ~/Sauvegardes/Quai."),
                  primaryButton: .destructive(Text("Désinstaller")) { Task { await quai.desinstaller(j) } },
                  secondaryButton: .cancel(Text("Annuler")))
        }
    }

    var enTete: some View {
        HStack(spacing: 14) {
            Circle().fill(quai.connecte == true ? Color.green : quai.connecte == false ? Color.red : Color.gray)
                .frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 2) {
                Text(quai.connecte == true ? "ROG Ally connectée" : quai.connecte == false ? "ROG Ally injoignable" : "Recherche de l'Ally…")
                    .font(.headline)
                Text(quai.connecte == false ? "Allume-la et vérifie qu'elle est sur le même Wi-Fi que le Mac."
                     : quai.session == "jeu" ? "En mode jeu" : quai.session == "bureau" ? "En mode Bureau" : " ")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if quai.connecte == true {
                Jauge(titre: "Interne", libre: quai.interne.libre, total: quai.interne.total)
                if let sd = quai.sd { Jauge(titre: "microSD", libre: sd.libre, total: sd.total) }
            }
            Button { Task { await quai.rafraichir() } } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).help("Rafraîchir")
            Button { reglages = true } label: { Image(systemName: "gearshape") }
                .buttonStyle(.borderless).help("Réglages")
        }
        .padding(.horizontal, 20).padding(.vertical, 12)
        .background(fond)
    }

    @ViewBuilder var zoneDepot: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch quai.etape {
            case .aucune:
                VStack(spacing: 10) {
                    Image(systemName: "arrow.down.to.line").font(.system(size: 30, weight: .light))
                    Text("Dépose un jeu ici").font(.title3)
                    Text(".exe, .msi, .zip, .7z, .rar, .iso, dossier, AppImage, Flatpak, ROM")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("Choisir un fichier…") { choisirFichier() }.disabled(quai.connecte != true)
                }
                .frame(maxWidth: .infinity, minHeight: 170)
                .background(RoundedRectangle(cornerRadius: 12).strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [6]))
                    .foregroundStyle(survol ? Color.accentColor : Color.secondary.opacity(0.5)))
            case .destination(let url, let taille):
                Carte {
                    Text(url.lastPathComponent).font(.headline)
                    Text("\(Quai.go(taille)) à envoyer. Où installer le jeu ?").foregroundStyle(.secondary)
                    HStack(spacing: 12) {
                        ChoixDest(titre: "Stockage interne", detail: "\(Quai.go(quai.interne.libre)) libres · plus rapide",
                                  conseille: quai.sd == nil || taille < 20_000_000_000) {
                            Task { await quai.envoyer(url, dest: "interne") } }
                        if let sd = quai.sd {
                            ChoixDest(titre: "microSD", detail: "\(Quai.go(sd.libre)) libres",
                                      conseille: taille >= 20_000_000_000) {
                                Task { await quai.envoyer(url, dest: "sd") } }
                        }
                    }
                    Button("Annuler") { quai.etape = .aucune }
                }
            case .envoi(let nom, let p):
                Carte {
                    Text("Envoi de \(nom) vers l'Ally…").font(.headline)
                    ProgressView(value: p)
                    Text("\(Int(p * 100)) %").font(.caption).foregroundStyle(.secondary)
                }
            case .confirmation:
                Carte {
                    Text("Prêt à installer").font(.headline)
                    TextField("Nom du jeu dans Steam", text: $quai.nom).textFieldStyle(.roundedBorder)
                    Text(quai.note).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    if quai.typeDetecte == "exe" {
                        Picker("", selection: $quai.modeExe) {
                            Text("C'est un installeur : je clique dans ses fenêtres sur l'Ally").tag("installeur")
                            Text("C'est le jeu lui-même, sans installation").tag("jeu")
                        }.pickerStyle(.radioGroup).labelsHidden()
                    } else if ["inno", "nsis", "msi", "archive", "iso"].contains(quai.typeDetecte) {
                        Toggle("Installer à la main (si l'installation automatique a déjà échoué)", isOn: $quai.forcerClics)
                            .font(.caption)
                    }
                    HStack {
                        Button("Installer") { Task { await quai.installer() } }
                            .keyboardShortcut(.defaultAction).disabled(quai.nom.trimmingCharacters(in: .whitespaces).isEmpty)
                        Button("Annuler") { Task { await quai.abandonner() } }
                    }
                }
            case .travail:
                Carte {
                    HStack { ProgressView().controlSize(.small); Text("Installation sur l'Ally…").font(.headline) }
                    if let p = quai.pct { ProgressView(value: p) }
                    Journal(lignes: quai.journal)
                }
            case .choixExe(let id, let cands):
                Carte {
                    Text("Quel programme lance le jeu ?").font(.headline)
                    Text("L'installation est faite. Choisis l'exécutable du jeu (en général le plus gros, au nom du jeu).")
                        .foregroundStyle(.secondary)
                    ForEach(cands) { c in
                        Button { Task { await quai.choisir(id, c) } } label: {
                            HStack {
                                Text(c.nom).fontWeight(.medium)
                                Text(Quai.go(c.taille)).foregroundStyle(.secondary)
                                Spacer()
                                Text(c.chemin.components(separatedBy: "/").suffix(3).dropLast().joined(separator: "/"))
                                    .font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.head)
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain).padding(.vertical, 4)
                        Divider()
                    }
                }
            case .interactif(let id, let msg):
                Carte {
                    Text("À toi de jouer sur l'Ally").font(.headline)
                    Text(msg).fixedSize(horizontal: false, vertical: true)
                    Button("J'ai terminé l'installation") { Task { await quai.interactifTermine(id) } }
                        .keyboardShortcut(.defaultAction)
                }
            case .fini(let msg):
                Carte {
                    Label(msg, systemImage: "checkmark.circle").font(.headline)
                    Journal(lignes: quai.journal)
                    Button("Installer un autre jeu") { quai.etape = .aucune }
                }
            case .erreur(let msg):
                Carte {
                    Label("Ça n'a pas marché", systemImage: "exclamationmark.triangle").font(.headline)
                    Text(msg).fixedSize(horizontal: false, vertical: true)
                    if !quai.journal.isEmpty { Journal(lignes: quai.journal) }
                    Button("Fermer") { quai.etape = .aucune }
                }
            }
        }
        .onDrop(of: [.fileURL], isTargeted: $survol) { items in
            guard quai.connecte == true, let item = items.first else { return false }
            _ = item.loadObject(ofClass: URL.self) { url, _ in
                if let url { DispatchQueue.main.async { quai.deposer(url) } }
            }
            return true
        }
    }

    var bibliotheque: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Installés par Quai").font(.headline)
            if quai.jeux.isEmpty {
                Text(quai.connecte == true ? "Aucun jeu pour l'instant." : "—").foregroundStyle(.secondary)
            }
            ForEach(quai.jeux) { j in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(j.nom).fontWeight(.medium)
                        Text([j.dest == "sd" ? "microSD" : "interne", Quai.go(j.taille), j.cree,
                              j.etat == "pret" ? nil : (j.enCours ? "installation en cours sur l'Ally" : "installation inachevée")]
                                .compactMap { $0 }.joined(separator: " · "))
                            .font(.caption).foregroundStyle(j.etat == "pret" ? Color.secondary : Color.orange)
                    }
                    Spacer()
                    if j.relancable && !j.enCours {
                        Button("Relancer") { Task { await quai.relancer(j) } }
                            .disabled(!quai.etapeLibre)
                            .help("Efface les restes de l'installation et relance l'installeur déjà sur l'Ally, sans renvoyer le fichier.")
                    }
                    Button("Désinstaller") { aDesinstaller = j }.disabled(!quai.etapeLibre || j.enCours)
                }
                .padding(.vertical, 6)
                Divider()
            }
        }
    }

    func choisirFichier() {
        let p = NSOpenPanel()
        p.canChooseDirectories = true
        p.allowsMultipleSelection = false
        p.prompt = "Envoyer"
        if p.runModal() == .OK, let u = p.url { quai.deposer(u) }
    }
}

struct Carte<Contenu: View>: View {
    @ViewBuilder var contenu: Contenu
    var body: some View {
        VStack(alignment: .leading, spacing: 10) { contenu }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .controlBackgroundColor)))
    }
}

struct ChoixDest: View {
    let titre: String, detail: String, conseille: Bool, action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 3) {
                HStack { Text(titre).fontWeight(.semibold); if conseille { Text("conseillé").font(.caption2).foregroundStyle(Color.accentColor) } }
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            .padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10).strokeBorder(conseille ? Color.accentColor : Color.secondary.opacity(0.4)))
            .contentShape(Rectangle())
        }.buttonStyle(.plain)
    }
}

struct Jauge: View {
    let titre: String, libre: Int64, total: Int64
    var body: some View {
        VStack(alignment: .trailing, spacing: 3) {
            Text("\(titre) · \(Quai.go(libre)) libres").font(.caption)
            ProgressView(value: total > 0 ? Double(total - libre) / Double(total) : 0).frame(width: 130)
        }
    }
}

struct Journal: View {
    let lignes: [String]
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(lignes.suffix(8).enumerated()), id: \.offset) { _, l in
                Text(l).font(.caption.monospaced()).foregroundStyle(.secondary).lineLimit(2)
            }
        }
    }
}

struct Reglages: View {
    @ObservedObject var quai: Quai
    @Environment(\.dismiss) var fermer
    @State var cle = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Jaquettes SteamGridDB").font(.headline)
            Text(quai.sgdb ? "Une clé est enregistrée sur l'Ally : les jeux reçoivent jaquette, bannière, logo et icône."
                 : "Sans clé, les jeux apparaissent dans Steam avec une tuile grise.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Text("Pour obtenir une clé : connecte-toi sur steamgriddb.com avec ton compte Steam, puis Préférences › API › Generate API Key.")
                .font(.caption).fixedSize(horizontal: false, vertical: true)
            Link("Ouvrir la page de la clé", destination: URL(string: "https://www.steamgriddb.com/profile/preferences/api")!)
            SecureField("Clé API", text: $cle).textFieldStyle(.roundedBorder)
            HStack {
                Spacer()
                Button("Fermer") { fermer() }
                Button("Enregistrer") { Task { await quai.enregistrerCle(cle); fermer() } }
                    .keyboardShortcut(.defaultAction).disabled(cle.trimmingCharacters(in: .whitespaces).count < 10)
            }
        }
        .padding(20).frame(width: 440)
        .background(fond)
    }
}

@main
struct QuaiApp: App {
    var body: some Scene {
        WindowGroup("Quai") { Fenetre() }
            .windowResizability(.contentMinSize)
            .commands { CommandGroup(replacing: .newItem) { } }
    }
}

