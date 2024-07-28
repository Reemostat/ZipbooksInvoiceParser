import streamlit as st
import os
import random
import json
import csv
import zipfile
import tempfile
from io import BytesIO
import base64
from PIL import Image
from dotenv import load_dotenv
import google.generativeai as genai
from PyPDF2 import PdfReader
from pdf2image import convert_from_path
import logging
import re

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
load_dotenv()
GEMINI_API_KEYS = os.getenv('GEMINI_API_KEYS').split(',')

# GEMINI_API_KEYS = ["abcd", "efg"] 

def get_random_api_key():
    return random.choice(GEMINI_API_KEYS)

def convert_pdf_to_images(pdf_file):
    logging.info("Starting PDF to image conversion")
    images = []
    
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
            temp_pdf.write(pdf_file.read())
            temp_pdf_path = temp_pdf.name

        # Convert PDF to images
        pdf_images = convert_from_path(temp_pdf_path)
        
        for i, image in enumerate(pdf_images):
            image_path = f"page_{i+1}.png"
            image.save(image_path, "PNG")
            images.append(image_path)
            logging.info(f"Saved page {i+1} as {image_path}")
        
    except Exception as e:
        logging.error(f"Error converting PDF to images: {str(e)}")
    finally:
        os.unlink(temp_pdf_path)
    
    logging.info("PDF to image conversion completed")
    return images

def image_to_bytes(image_path):
    with open(image_path, "rb") as image_file:
        return image_file.read()

def create_gemini_model():
    generation_config = {
        "temperature": 0.35,
        "top_p": 0.95,
        "top_k": 64,
        "max_output_tokens": 8192,
    }
    return genai.GenerativeModel(
        model_name="gemini-1.5-pro",
        generation_config=generation_config,
    )

def process_file(file):
    if file.type == "application/pdf":
        images = convert_pdf_to_images(file)
    else:  # Assume it's an image
        image = Image.open(file)
        temp_image_path = "temp_image.png"
        image.save(temp_image_path, format="PNG")
        images = [temp_image_path]
    
    image_bytes_list = [{"mime_type": "image/png", "data": image_to_bytes(path)} for path in images]
    
    genai.configure(api_key=get_random_api_key())
    model = create_gemini_model()
    
    # Process JSON
    json_prompt = """
        You are an expert at reading invoices and extracting their data. Analyze the given invoice image(s) and structure all the information into a single, comprehensive JSON object. Follow these guidelines:

        1. Use the structure below as a base template, but adapt it intelligently to match the actual content of the invoice. Basically, the sections in the invoice will be - Invoice details (invoice number, issue date, due date, etc), client or customer info (often in the billed_to and ship to section), company info, items, and summary

        {
          "invoice_number": "",
          "issue_date": "",
          "due_date": "",
          "payment_terms": "",
          "customer_info": { 
            "name": "",
            "address": { (or bill to / ship to address)
              "line_1": "" (house number and street)
              "line_2": "" (city, state, country)
              "pin_code": "" (sometimes also called postal code / zip code. eg: 380015)
            }, 
            "contact": {
              "phone": "",
              "email": ""
            }
          },
          "company_info": { (info about the company issuing the invoice)
            "name": "", 
            "address": { (or bill to / ship to address)
              "line_1": "" (house number and street)
              "line_2": "" (city, state, country)
              "pin_code": "" (sometimes also called postal code / zip code. eg: 380015)
            },
            "contact": {
              "phone": "",
              "email": "",
              "website": ""
            },
            "GSTIN": ""
          },
          "items": [
            {
              "description": "",
              "quantity": "",
              "unit_price": "",
              "total": ""
            },
            {
              "description": "",
              "quantity": "",
              "unit_price": "",
              "total": ""
            }, 
          ],
          "summary": {
            "subtotal": "",
            "tax": {
              "rate": "", (in percent)
              "amount": "" 
            },
            "discount": {
              "rate": "", (in percent)
              "amount": ""
            },
            "invoice_total": "" (subtotal - discount amount + tax amount)
          },
          "notes": ""
        }

        2. If you encounter information that doesn't fit into this structure, create new appropriate fields or nested objects to accommodate it. Use clear, descriptive field names.

        3. If certain information is not present in the invoice, include them with null or empty values.

        4. Ensure all monetary values are consistent in format (e.g., always use a decimal point and include cents, like "100.00").

        5. For arrays like "items", include all relevant items found in the invoice.

        Important: The response should be a single, valid JSON object only. Do not include any explanatory text, quotes, or backticks around the JSON. Ensure the output can be directly parsed by a JSON parser.
    """
    json_response = model.generate_content(image_bytes_list + [json_prompt])
    try:
        # Strip out backticks and any text before the actual JSON
        json_text = json_response.text.strip()
        json_text = re.sub(r'^```json\s*', '', json_text)
        json_text = re.sub(r'\s*```$', '', json_text)
        
        extracted_data = json.loads(json_text)
        logging.info("JSON data extracted successfully")
    except json.JSONDecodeError as e:
        logging.error(f"Error processing JSON response: {str(e)}")
        logging.error(f"Raw response: {json_response.text[:500]}...")
        
        # Save the raw response for debugging
        with open('invalid_json_response.txt', 'w') as f:
            f.write(json_response.text)
        logging.info("Raw response saved to invalid_json_response.txt")
        extracted_data = {}  # Set to empty dict if parsing fails

        # Clean up temporary image file
        for image_path in images:
            if os.path.exists(image_path):
                os.remove(image_path)

    
    # Process summary
    summary_prompt = """
    Analyze the given invoice image(s) and provide a comprehensive summary. Include the following details:
    1. A brief introduction stating what this document is.
    2. Key information such as invoice number, date, and total amount.
    3. Details about the billed client and the company issuing the invoice.
    4. A summary of the items or services listed, including the total number of items and any notable entries.
    5. Information about subtotal, discounts (if any), tax rates, and final total.
    6. Any additional relevant information or unusual aspects of this invoice.

    Present this information in a clear, concise paragraph format that gives a complete overview of the invoice contents.
    """
    summary_response = model.generate_content(image_bytes_list + [summary_prompt])
    summary_text = summary_response.text
    
    # Process CSV
    csv_prompt = """
    Provide details about the items and summary information from the invoice image(s) in CSV format. Follow these guidelines:

    1. Use the following columns: Description, Quantity, Unit Price, Amount
    2. For actual items purchased:
      - Fill in all columns appropriately
      - Use the 'Quantity' column for the quantity of each item
      - 'Unit Price' should be the price per unit
      - 'Amount' should be the total for that line item
    3. For summary information (subtotal, tax, discount, total):
      - Put the label (e.g., "Subtotal", "Discount", "Tax Rate", "Tax", "TOTAL") in the 'Unit Price' column
      - Leave 'Description' and 'Quantity' columns empty for these rows
      - Put the corresponding amount or rate/percentage in the 'Amount' column
    4. Ensure all monetary values are formatted consistently (e.g., always use a dollar sign (except for percentages where you'll use %) and two decimal places)
    5. Do not include any headers or explanatory text outside the CSV data

    Example structure:
    Description,Quantity,Unit Price,Amount
    Item 1,2,$10.00,$20.00
    Item 2,1,$15.00,$15.00
    Subtotal,,,35.00
    ,,Tax Rate(%),0 (the tax rate in percent. make sure to put this value in the "amount" column)
    ,,Tax Amount,0 (the subtotal times the tax rate. make sure to put this value in the "amount" column)
    ,,TOTAL,38.50 (if not given then it is calculated as follows: subtotal - discount + tax_amount. if discount is not mentioned in the summary then assume it to be 0)

    Ensure the CSV structure accurately represents the invoice data and follows these guidelines.
    """
    csv_response = model.generate_content(image_bytes_list + [csv_prompt])
    csv_text = csv_response.text
    
    return extracted_data, summary_text, csv_text

def create_download_link(file_content, filename):
    b64 = base64.b64encode(file_content).decode()
    return f'<a href="data:application/octet-stream;base64,{b64}" download="{filename}">Download {filename}</a>'

def main():
    img = Image.open('./Logo/logo4.png')
    st.image(img)
    st.title("Zipbooks Invoice Parser")
    st.write("Upload your invoice PDF or image to extract key details.")
    
    uploaded_file = st.file_uploader("Choose an invoice PDF or image", type=["pdf", "png", "jpg", "jpeg"])
    # uploaded_file = st.file_uploader("Choose an invoice PDF or image", type=["pdf", "png"])
    
    if uploaded_file is not None:
        if st.button("Process Invoice"):
            with st.spinner("Processing..."):
                extracted_data, summary_text, csv_text = process_file(uploaded_file)
                
                # Create a zip file containing all outputs
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
                    zip_file.writestr("extracted_data.json", json.dumps(extracted_data, indent=2))
                    zip_file.writestr("summary.txt", summary_text)
                    zip_file.writestr("invoice_items.csv", csv_text)
                
                # Create download link for zip file
                st.markdown(create_download_link(zip_buffer.getvalue(), "invoice_outputs.zip"), unsafe_allow_html=True)
                
                # Display summary
                st.subheader("Invoice Summary")
                st.write(summary_text)
                
                # Display JSON data
                st.subheader("Extracted Data (JSON)")
                st.json(extracted_data)
                
                # Display CSV data
                st.subheader("Invoice Items (CSV)")
                st.write(csv_text)

if __name__ == "__main__":
    main()