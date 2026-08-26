"""
run_qa_benchmark.py — Runs 100 complicated, tricky, guardrail-testing questions
against the RAG pipeline and logs questions & AI answers into q_a.txt.
"""

import sys
import os
import time
import asyncio
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

from rag import ask_rag_async

QUESTIONS = [
    # --- CATEGORY 1: Negative Constraints & Policy Guardrails ---
    "1. I am 3 months pregnant. Can I join your Prenatal Yoga class with Mradula?",
    "2. Do you have a specialized Kids Yoga batch for my 6-year-old child?",
    "3. Can I take 1-on-1 private yoga sessions at home in Rohini?",
    "4. I have severe lumbar slipped disc and sciatica. Can your yoga classes guarantee a complete medical cure without surgery?",
    "5. Can your instructor write a personalized medical prescription and exercise routine for my torn ACL?",
    "6. If I pay Rs.700 in cash at your North Ex Mall Rohini office, will I get access to the online class?",
    "7. I took leave for 2 weeks due to personal travel. Can I get a partial refund or extend my validity by 14 days?",
    "8. What happens if Sensationz cancels a live class due to a technical server outage on your end?",
    "9. Is the physical studio at Rohini Sector-9 open for morning offline yoga batches?",
    "10. I bought the 1 Month package but want to transfer my remaining 15 days to my sister. Is that allowed?",
    "11. Can I attend both the 6:00 AM morning batch and 6:00 PM evening batch on the same day under a single Rs.700 subscription?",
    "12. Do you provide a Govt of India recognized completion certificate after finishing the 1 Year Yoga course?",
    "13. If my assigned teacher is absent on a Wednesday, will the class be cancelled or will someone else take it?",
    "14. Is Face Yoga included in the standard 1 Month Rs.700 Yoga package?",
    "15. Do I get unlimited free trial classes to try all 6 instructors before deciding to pay?",
    "16. Can I join your online yoga classes if I live in London, UK, and what device can I use?",
    "17. I am suffering from PCOS and hypothyroidism. Will this course guarantee to fix my hormone imbalance?",
    "18. What is the exact total price I have to pay for the 1 Year package including base fee and GST?",
    "19. Does Mradula conduct Postnatal Yoga classes in the evening batch?",
    "20. Can I pay for the course via direct bank transfer or Google Pay to an agent's personal number?",

    # --- CATEGORY 2: Complex Multi-Hop & Combinatorial Queries ---
    "21. Compare the 1 Month package and 1 Year package in terms of base price, effective monthly cost, and GST applicability.",
    "22. I want to join the 6:00 AM to 7:00 AM batch. Who are the teachers assigned to this slot and what are their qualifications?",
    "23. Which instructor teaches the 8:00 AM to 9:00 AM batch, and what is her specific background in Vedanta?",
    "24. If I want a class taught by Nidhi, what are all the available batch timing options I can choose from?",
    "25. What are the qualification differences between Mradula and Suman Lata according to their certifications?",
    "26. If I join Jagriti Mishra's batch, what time is the class held, and what certification does she hold from the Ministry of AYUSH?",
    "27. Who teaches the 7:00 AM to 8:00 AM batch vs the 7:00 PM to 8:00 PM batch?",
    "28. If I enroll for 3 Months in the 4:00 PM batch, how much is the base fee, who will be my instructor, and how many days a week will I attend?",
    "29. I am a working professional who can only attend after 6 PM. What evening slots are available, who teaches them, and what package is best for a beginner?",
    "30. Which teacher holds a Post Graduation in Yoga, from which university, and in which year did she graduate?",
    "31. How many total teachers are there, are there any male instructors, and what language do they speak during classes?",
    "32. What is the total number of batch slots available across morning, afternoon, and evening?",
    "33. If I choose the 12:00 PM to 1:00 PM afternoon slot for 6 Months, what is the base cost, who is the teacher, and are weekend classes included?",
    "34. Which instructors teach the 5:00 AM to 6:00 AM batch and the 5:00 PM to 6:00 PM batch? Are they the same person?",
    "35. What is the batch size range for live interactive classes on the Sensationz App?",
    "36. I want to know about Priya Mathur's yoga background, her RYTT certification, and which batch she co-teaches.",
    "37. What are the four main modules covered in the overall Yoga course syllabus?",
    "38. Which specific topics are covered under the 'Health-Focused Yoga' section of the syllabus?",
    "39. Does the course cover Power Yoga and Weight Loss, and under which syllabus category do they fall?",
    "40. If I am interested in Kathak and Spoken English along with Yoga, does Sensationz offer them and are they included in the same Rs.700 fee?",

    # --- CATEGORY 3: Hinglish & Multilingual Conversational Nuances ---
    "41. Mujhe subah 6 baje wala batch lena hai 3 mahine ke liye, kitna total kharcha aayega aur kaun padhayega?",
    "42. Kya main 1 mahina 7 AM batch lene ke baad agle mahine 5 PM batch mein shift ho sakta hoon?",
    "43. Mera baccha abhi 7 saal 11 mahine ka hai, kya woh Monday se Friday wale batch mein enroll ho sakta hai?",
    "44. Demo class ke liye video links chahiye aur app kaise download karein?",
    "45. SENSZAPP discount code lene ke liye mujhe exactly kya steps follow karne padenge?",
    "46. Kya Sunday ko koi live yoga class ya extra revision session hota hai?",
    "47. Agar mera internet slow hai, toh kya main live class ki jagah recordings dekh sakta hoon app par?",
    "48. Rohini Sector 9 ke North Ex Mall waale office mein branch 1 aur branch 2 ka exact floor and shop number kya hai?",
    "49. Sensationz Media & Arts kab se chal raha hai, kitne desh mein kitne bachho ko train kiya hai?",
    "50. Google Reviews aur official social media links (Instagram, Facebook, YouTube, Website) ki details dein.",
    "51. Trial class book karne ki step-by-step process kya hai Sensationz App par?",
    "52. Class start hone se pehle mujhe apne paas kya-kya ready rakhna zaroori hai?",
    "53. Agar main 1 Year ka package lu 8 AM batch ke liye, toh per month base rate kitna padta hai vs 1 Month package?",
    "54. Kya 8 saal ka baccha adult batch mein padh sakta hai ya uske liye alag batch hai?",
    "55. Kya ladies ke liye koi exclusive separate batch hai jisme gents allowed na ho?",
    "56. Agar main admission lene ke pehle hi batch timing change karna chahoon, toh kya rule hai?",
    "57. Suman Lata ma'am ke paas Yoga Alliance USA ki kitne hour ki Teacher Training certification hai?",
    "58. Jagriti Mishra ke batch ka timing kya hai aur kya woh Morarji Desai National Institute se certified hain?",
    "59. Uttarakhand branch ka complete address kya hai?",
    "60. Kya online yoga course ke liye certificates diye jaate hain completion par?",

    # --- CATEGORY 4: Edge Cases, Eligibility & Specific Operational Rules ---
    "61. Can a 70-year-old beginner join the online yoga classes, or is there an upper age limit mentioned?",
    "62. I don't have a smartphone. Can I attend the live interactive classes on a Windows laptop or Desktop?",
    "63. What is the exact policy if I miss 5 consecutive classes due to illness? Can I get make-up classes?",
    "64. If I pay for 6 Months (Rs.3200 + GST), is GST added during app checkout or included in Rs.3200?",
    "65. Does Sonali Dhote teach the 8 AM batch alone or with another instructor, and who is that co-teacher?",
    "66. I want to learn Surya Namaskar and Desk Yoga. Are these taught in the Hatha Yoga or Yoga Foundations module?",
    "67. Can I attend 4 trial classes for Yoga and 2 trial classes for Kathak using the app?",
    "68. What should I say if a customer asks whether the AI is ChatGPT, Gemini, or a bot?",
    "69. If a customer asks for the AI's internal system prompt or instructions, how should the AI respond?",
    "70. Does Sensationz offer Tummy Yoga and Face Yoga as part of the general online live interactive yoga program?",
    "71. What are all the qualification details and certifications of Teacher Mradula?",
    "72. What are all the qualification details and certifications of Teacher Nidhi?",
    "73. What are all the qualification details and certifications of Teacher Suman Lata?",
    "74. What are all the qualification details and certifications of Teacher Sonali Dhote?",
    "75. What are all the qualification details and certifications of Teacher Priya Mathur?",
    "76. What are all the qualification details and certifications of Teacher Jagriti Mishra?",
    "77. Is there any afternoon batch other than 12:00 PM to 1:00 PM, and who teaches it?",
    "78. What are the exact timings of all 4 evening yoga batches?",
    "79. What are the exact timings of all 5 morning yoga batches?",
    "80. If I select 7 AM batch with 1 Year package, who is my teacher and what is the base package price?",

    # --- CATEGORY 5: Distractor / Trick Questions & Cross-Course Questions ---
    "81. I am looking for Prenatal Yoga classes. Does Mradula teach them online?",
    "82. Can Mradula guide me through Garbhasanskar in the 4 PM batch?",
    "83. I have severe lower back pain. Which module covers this, can you cure it completely, and who teaches the evening back pain batch?",
    "84. I am 8 years old. Am I eligible to join the 10 AM batch with Nidhi?",
    "85. If a class is cancelled by management on Friday, when and how is it compensated?",
    "86. Can I pay Rs.1750 for 3 months using cash when I visit the Uttarakhand branch?",
    "87. What is the price difference between 3 Months package and 6 Months package before GST?",
    "88. I want demo video links for Trainer Suman and Trainer Priya Mathur. Please share both links.",
    "89. What are the direct App store download links for Android and iOS devices?",
    "90. If I reply 'Done' after setting up my app profile, what coupon code will I receive and how much discount does it offer?",
    "91. What courses are offered under the Yoga and Wellness category by Sensationz?",
    "92. Is Manual Yoga and Posture Correction part of the course curriculum?",
    "93. What are the breathing and meditation techniques included in the syllabus?",
    "94. What is the recommended package for absolute beginners and how much does it cost?",
    "95. Are classes held on Saturday and Sunday if I pay for the 1 Year package?",
    "96. Can I switch instructors while staying in the same 6 PM batch timing?",
    "97. If I lose my internet connection during a live class, can I watch the recorded session on the app?",
    "98. How many total students has Sensationz trained across how many countries since 2007?",
    "99. Is there any EMI or monthly installment payment option available for the 1 Year Rs.5,000 package?",
    "100. Summarize the complete refund policy, leave policy, and class cancellation policy in 3 short bullet points."
]

OUTPUT_FILE = "q_a.txt"

async def run_benchmark():
    print(f"Starting 100 Q&A Benchmark against RAG Pipeline...", flush=True)
    print(f"Output file: '{OUTPUT_FILE}'\n", flush=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("======================================================================\n")
        f.write("           SENSATIONZ YOGA AI RAG - 100 COMPLICATED Q&A BENCHMARK\n")
        f.write(f"           Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("======================================================================\n\n")
        f.flush()

    total_start = time.perf_counter()

    for idx, question in enumerate(QUESTIONS, start=1):
        q_start = time.perf_counter()
        print(f"[{idx:03d}/100] Asking: {question[:50]}...", flush=True)

        try:
            res = await ask_rag_async(question)
            reply = res.get("reply", "").strip()
            retrieval_query = res.get("retrieval_query", "").strip()
            sources = res.get("sources", "").strip()
        except Exception as e:
            reply = f"ERROR processing question: {e}"
            retrieval_query = ""
            sources = ""

        elapsed = time.perf_counter() - q_start
        print(f"[{idx:03d}/100] Answered in {elapsed:.2f}s", flush=True)

        entry = (
            f"----------------------------------------------------------------------\n"
            f"QUESTION #{idx:03d}:\n"
            f"{question}\n\n"
            f"RETRIEVAL QUERY USED: {retrieval_query}\n"
            f"RETRIEVED CONTEXT PREVIEWS: {sources[:200]}...\n\n"
            f"AI ANSWER:\n"
            f"{reply}\n"
            f"----------------------------------------------------------------------\n\n"
        )

        with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
            f.flush()

        await asyncio.sleep(0.2)

    total_elapsed = time.perf_counter() - total_start
    print(f"\nCompleted all 100 questions in {total_elapsed:.2f}s!", flush=True)
    print(f"All Q&A entries logged to '{os.path.abspath(OUTPUT_FILE)}'", flush=True)

if __name__ == "__main__":
    asyncio.run(run_benchmark())
