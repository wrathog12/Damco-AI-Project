"use client";

import { useState } from "react";

interface SchemeCardProps {
  scheme: any;
  onClose: () => void;
}

const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "hi", label: "Hindi" },
  { code: "bn", label: "Bengali" },
  { code: "mr", label: "Marathi" },
];

export default function SchemeCard({ scheme, onClose }: SchemeCardProps) {
  const [lang, setLang] = useState("en");

  // Helper to get translated string, falling back to English
  const t = (key: string) => {
    if (lang === "en" || !scheme.translations || !scheme.translations[lang]) {
      return scheme[key] || "";
    }
    return scheme.translations[lang][key] || scheme[key] || "";
  };

  // Helper to get translated array
  const tArray = (key: string, subKey?: string) => {
    const original = scheme[key] || [];
    if (lang === "en" || !scheme.translations || !scheme.translations[lang]) {
      return original;
    }
    const translatedArray = scheme.translations[lang][key];
    if (!translatedArray || !Array.isArray(translatedArray)) return original;
    
    // If it's an array of objects, merge translated fields
    if (subKey && translatedArray.length > 0 && typeof translatedArray[0] === 'object') {
       return original.map((item: any, i: number) => ({
         ...item,
         [subKey]: translatedArray[i] ? translatedArray[i][subKey] : item[subKey]
       }));
    }
    return translatedArray;
  };

  const name = t("scheme_name");
  const description = t("description");
  const eligibility = t("eligibility_description");
  const process = t("application_process");
  const benefits = tArray("benefits", "description");
  const docs = tArray("documents_required", "name");

  // Determine available languages based on the scheme's state
  const state = scheme.metadata?.state;
  const availableLangs = ["en", "hi"];
  if (state === "West Bengal") availableLangs.push("bn");
  if (state === "Maharashtra") availableLangs.push("mr");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 md:p-8 bg-slate-900/40 backdrop-blur-md animate-in fade-in duration-200">
      <div className="bg-white rounded-[24px] shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col overflow-hidden ring-1 ring-black/5 animate-in zoom-in-95 duration-300">
        
        {/* Header */}
        <div className="px-8 py-8 border-b border-gray-100 flex flex-col md:flex-row md:items-start justify-between bg-white relative">
          <div className="flex-1 pr-6">
            <div className="flex items-center gap-3 mb-4">
              <span className="px-3 py-1 rounded-full bg-blue-50 text-blue-600 text-sm font-semibold tracking-wide uppercase">
                {scheme.metadata?.state || "Government Scheme"}
              </span>
              <span className="px-3 py-1 rounded-full bg-slate-50 text-slate-600 text-sm font-semibold tracking-wide uppercase border border-slate-100">
                {scheme.metadata?.category}
              </span>
            </div>
            <h2 className="text-3xl font-extrabold text-slate-900 leading-tight tracking-tight">
              {name}
            </h2>
            {scheme.ministry && (
              <p className="text-base text-slate-500 mt-2 font-medium">{scheme.ministry}</p>
            )}
          </div>
          
          <div className="flex items-center gap-4 mt-6 md:mt-0">
            {/* Language Selector */}
            <select
              value={lang}
              onChange={(e) => setLang(e.target.value)}
              className="text-base border-gray-200 rounded-xl bg-slate-50 shadow-sm focus:ring-blue-500 focus:border-blue-500 py-2.5 pl-4 pr-10 font-medium cursor-pointer transition-colors hover:bg-slate-100"
            >
              {LANGUAGES.filter(l => availableLangs.includes(l.code)).map(l => (
                <option key={l.code} value={l.code}>{l.label}</option>
              ))}
            </select>
            
            <button 
              onClick={onClose}
              className="p-3 text-slate-400 hover:text-slate-700 hover:bg-slate-100 rounded-full transition-colors"
            >
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M18 6 6 18"/><path d="m6 6 12 12"/>
              </svg>
            </button>
          </div>
        </div>

        {/* Content Body */}
        <div className="flex-1 overflow-y-auto px-8 py-8 space-y-10 custom-scrollbar">
          
          {/* Top section: Description & Eligibility side-by-side if on desktop */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-10">
            <div>
              <h3 className="text-xl font-bold text-slate-900 mb-3">Overview</h3>
              <p className="text-slate-600 text-base leading-relaxed whitespace-pre-wrap">{description}</p>
            </div>
            
            {eligibility && (
              <div>
                <h3 className="text-xl font-bold text-slate-900 mb-3">Eligibility</h3>
                <p className="text-slate-600 text-base leading-relaxed whitespace-pre-wrap">{eligibility}</p>
              </div>
            )}
          </div>

          {/* Benefits */}
          {benefits && benefits.length > 0 && (
            <div>
              <h3 className="text-xl font-bold text-slate-900 mb-4">Key Benefits</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {benefits.map((b: any, i: number) => (
                  <div key={i} className="flex items-start gap-4 p-5 rounded-2xl bg-blue-50/40 border border-blue-100/50">
                    <div className="w-8 h-8 rounded-full bg-blue-100 flex items-center justify-center flex-shrink-0 mt-0.5">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" className="text-blue-600">
                        <polyline points="20 6 9 17 4 12"/>
                      </svg>
                    </div>
                    <div>
                      {b.amount && <div className="text-lg font-bold text-blue-700 mb-1">₹{b.amount}</div>}
                      <p className="text-base text-slate-700 leading-relaxed">{b.description}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Documents & Process */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-10 pt-6 border-t border-slate-100">
            {docs && docs.length > 0 && (
              <div>
                <h3 className="text-xl font-bold text-slate-900 mb-4">Documents Required</h3>
                <ul className="space-y-4">
                  {docs.map((d: any, i: number) => (
                    <li key={i} className="text-base text-slate-600 flex items-start gap-3">
                      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-300 flex-shrink-0 mt-0.5">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
                      </svg>
                      <span>
                        {d.name} {d.is_mandatory && <span className="text-rose-500 font-medium ml-1 text-sm bg-rose-50 px-2 py-0.5 rounded">*Required</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {process && (
              <div>
                <h3 className="text-xl font-bold text-slate-900 mb-4">Application Process</h3>
                <div className="text-base text-slate-600 whitespace-pre-wrap leading-relaxed">
                  {process}
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="px-8 py-5 bg-white border-t border-gray-100 flex flex-col md:flex-row justify-between items-center gap-4">
          <p className="text-sm text-slate-500 font-medium">
            Source: <a href={scheme.source_url} target="_blank" rel="noreferrer" className="text-blue-600 hover:text-blue-700 hover:underline">Official Govt. Portal</a>
          </p>
          <div className="flex gap-4 w-full md:w-auto">
            <button 
              onClick={onClose}
              className="flex-1 md:flex-none px-6 py-3.5 text-base font-bold text-slate-600 bg-white border-2 border-slate-200 rounded-xl hover:bg-slate-50 hover:text-slate-900 transition-colors"
            >
              Dismiss
            </button>
            {scheme.application_url && (
              <a 
                href={scheme.application_url}
                target="_blank"
                rel="noreferrer"
                className="flex-1 md:flex-none px-8 py-3.5 text-base font-bold text-white bg-blue-600 rounded-xl hover:bg-blue-700 transition-all flex items-center justify-center gap-2 shadow-lg shadow-blue-200/50"
              >
                Apply Online
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
                </svg>
              </a>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
